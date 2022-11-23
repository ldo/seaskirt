#+
# Various useful Asterisk-related definitions.
#
# Copyright © 2007-2022 by Lawrence D'Oliveiro <ldo@geek-central.gen.nz>.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library, in a file named COPYING; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor,
# Boston, MA 02110-1301, USA
#-

import sys
import os
import socket

#+
# Useful stuff
#-

def naturals() :
    "returns the sequence of natural numbers. May be used as a" \
    " unique-id generator."
    i = 0
    while True :
        i += 1
        yield i
    #end while
#end naturals

#+
# Asterisk Manager Interface
#-

class Manager :
    "simple management of an Asterisk Manager API connection."

    NL = "\015\012" # protocol line delimiter

    auto_multi_response = \
      { # table of actions which are automatically recognized as being multi_response.
        # Values are event types indicating last response to that action.
        # Note keys are lowercase, while values are case-sensitive.
        "agents" : "AgentsComplete",
        "bridgelist" : "BridgeListComplete",
        "devicestatelist" : "DeviceStateListComplete",
        "extensionstatelist" : "ExtensionStateListComplete",
        "faxsessions" : "FAXSessionsComplete",
        "iaxpeerlist" : "PeerlistComplete",
        "iaxpeers" : "PeerlistComplete",
        "iaxregistry" : "RegistrationsComplete",
        "parkedcalls" : "ParkedCallsComplete",
        "parkinglots" : "ParkinglotsComplete",
        "pjsipshowauths" : "AuthListComplete",
        "pjsipshowcontacts" : "ContactListComplete",
        "pjsipshowendpoints" : "EndpointListComplete",
        "pjsipshowregistrationinboundcontactstatuses" : "ContactStatusDetailComplete",
        "pjsipshowregistrationsinbound" : "InboundRegistrationDetailComplete",
        "pjsipshowregistrationsoutbound" : "OutboundRegistrationDetailComplete",
        "pjsipshowsubscriptionsinbound" : "InboundSubscriptionDetailComplete",
        "pjsipshowsubscriptionsoutbound" : "OutboundSubscriptionDetailComplete",
        "presencestatelist" : "PresenceStateListComplete",
        "queuestatus" : "QueueStatusComplete",
        "sippeers" : "PeerlistComplete",
        "status" : "StatusComplete",
        "coreshowchannels" : "CoreShowChannelsComplete",
      }

    @classmethod
    def sanitize(celf, parm) :
        # sanitizes the value of parm to avoid misbehaviour with Manager API syntax.
        return str(parm).replace("\n", "")
    #end sanitize

    def send_request(self, action, parms, vars = None) :
        "sends a request to the Manager, leaving it up to you to retrieve" \
        " any subsequent response with get_response."
        to_send = "Action: " + action + self.NL
        if self.id_gen != None :
            to_send += "ActionID: %d%s" % (next(self.id_gen), self.NL)
        #end if
        for parm in parms.keys() :
            to_send += parm + ": " + self.sanitize(parms[parm]) + self.NL
        #end for
        if vars != None :
            for var in vars.keys() :
                to_send += \
                    "Variable: " + self.sanitize(var) + "=" + self.sanitize(vars[var]) + self.NL
            #end for
        #end if
        to_send += self.NL # marks end of request
        if self.debug :
            sys.stderr.write(to_send)
        #end if
        to_send = to_send.encode()
        while len(to_send) != 0 :
            sent = self.the_conn.send(to_send)
            to_send = to_send[sent:]
        #end while
    #end send_request

    def get_response(self) :
        "reads and parses another response from the Asterisk Manager connection." \
        " This can be a reply to a prior request, or it can be an unsolicited event" \
        " notification, if you have enabled those on this connection."
        response = {}
        while True :
            split = self.buff.split(self.NL, 1)
            if len(split) == 2 :
                self.buff = split[1]
                if len(split[0]) == 0 :
                    break
                (keyword, value) = split[0].split(": ", 1)
                response[keyword] = value
            else :
                if self.debug :
                    sys.stderr.write("Getting more\n")
                #end if
                more = self.the_conn.recv(4096)
                if len(more) == 0 :
                    self.EOF = True
                    break
                #end if
                self.buff += more.decode()
                if self.debug :
                    sys.stderr.write \
                      (
                        "Got (%u): \"%s\"\n" % (len(self.buff), self.buff)
                      )
                #end if
            #end if
        #end while
        return response
    #end get_response

    def got_more_response(self) :
        "returns True iff there’s another response from the Asterisk Manager" \
        " connection in the buffer waiting to be parsed and returned."
        return len(self.buff.split(self.NL + self.NL, 1)) == 2
    #end got_more_response

    def transact(self, action, parms, vars = None) :
        "does a basic transaction and returns the single response" \
        " or sequence of responses. Note this doesn’t currently handle" \
        " commands like “IAXpeers” or “Queues” that don’t return" \
        " response lines in the usual “keyword: value” format."
        self.send_request(action, parms, vars)
        multi_response = self.auto_multi_response.get(action.lower())
        if multi_response != None :
            response = []
            first_response = True
            while True :
                next_response = self.get_response()
                if self.EOF or len(next_response) == 0 :
                    break
                if self.debug :
                    sys.stderr.write \
                      (
                        "next_response: \"%s\"\n" % repr(next_response)
                      )
                #end if
                if first_response :
                    # check for success/failure
                    if not next_response.get("Response", None) == "Success" :
                        raise RuntimeError \
                          (
                            "%s failed -- %s" % (action, next_response.get("Message", "?"))
                          )
                    #end if
                    first_response = False
                else :
                    response.append(next_response)
                    if (
                            type(multi_response) == str
                        and
                            next_response.get("Event", None) == multi_response
                    ) :
                        break
                #end if
            #end while
        else :
            response = self.get_response()
        #end if
        return response
    #end transact

    def authenticate(self, username, password, want_events = False) :
        "logs in with a username and password. This is mandatory" \
        " after opening the connection, before trying any other" \
        " commands. want_events indicates whether you want to receive" \
        " unsolicited event notifications on this connection."
        parms = \
            {
                "Username" : username,
                "Secret" : password,
            }
        if not want_events :
            parms["Events"] = "off"
        #end if
        response = self.transact \
          (
            action = "Login",
            parms = parms
          )
        if response["Response"] != "Success" :
            raise RuntimeError("authentication failed")
        #end if
    #end authenticate

    def do_command(self, command) :
        "does a Command request and returns the response text."
        self.send_request("Command", {"Command" : command})
        response = ""
        first_response = True
        status = None
        while True :
            while True :
                if self.buff.find(self.NL) >= 0 or self.EOF :
                    break
                if self.debug :
                    sys.stderr.write("Getting more\n")
                #end if
                more = self.the_conn.recv(4096)
                if len(more) == 0 :
                    print("EOF hit with buff = %s" % repr(self.buff)) # debug
                    self.EOF = True
                    break
                #end if
                self.buff += more.decode()
                if self.debug :
                    sys.stderr.write \
                      (
                        "Got (%u): \"%s\"\n" % (len(self.buff), self.buff)
                      )
                #end if
            #end while
            if self.buff.find(self.NL) < 0 :
                break
            line, self.buff = self.buff.split(self.NL, 1)
            if len(line) == 0 :
                break
            items = line.split(": ", 1)
            if len(items) == 2 :
                if items[0] == "Response" :
                    assert first_response
                    status = items[1]
                    if status not in ("Follows", "Success") :
                        raise RuntimeError \
                          (
                            "Command failed -- %s" % (status,)
                          )
                    #end if
                    first_response = False
                elif items[0] == "Output" :
                    assert not first_response
                    response += items[1] + "\n"
                #end if
            #end if
        #end while
        return response
    #end do_command

    def get_queue_status(self) :
        "does a QueueStatus request and returns the parsed response as a list" \
        " of entries, one per queue."
        response = self.transact("QueueStatus", {})
        result = {}
        responses = iter(response)
        last_queue = None # to begin with
        while True :
            response_item = next(responses, None)
            if response_item != None :
                kind = response_item.get("Event") # absent for first response item
            else :
                kind = "QueueParams" # dummy to finish entry for last queue
            #end if
            if kind == "QueueParams" :
                if last_queue != None :
                    last_queue["members"] = last_queue_members
                    last_queue["entries"] = last_queue_entries
                    result[last_queue_name] = last_queue
                #end if
                if response_item == None :
                    break
                last_queue_name = response_item["Queue"]
                last_queue = dict(response_item)
                last_queue_members = []
                last_queue_entries = []
            elif kind == "QueueMember" :
                last_queue_members.append(dict(response_item))
            elif kind == "QueueEntry" :
                last_queue_entries.append(dict(response_item))
            #end if
        #end while
        return result
    #end get_queue_status

    def get_channels(self) :
        "gets information on all currently-existing channels."
        result = []
        fields = \
            (
                "channel",
                "context",
                "exten",
                "prio",
                "state",
                "appl",
                "data",
                "cid",
                "accountcode",
                "amaflags",
                "duration",
                "bridged_context",
              )
        for line in self.do_command("core show channels concise").split("\012") :
            line = line.split("!")
            if len(line) >= len(fields) :
                result.append(dict(zip(fields, line)))
            #end if
        #end for
        return result
    #end get_channels

    def __init__(self, host = "127.0.0.1", port = 5038, *, id_gen = None, timeout = None, debug = False) :
        "opens connection and receives initial Hello message" \
        " from Asterisk."
        self.debug = debug
        self.the_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.id_gen = id_gen
        if timeout != None :
            self.the_conn.settimeout(timeout)
        #end if
        self.the_conn.connect((host, port))
        self.buff = ""
        self.EOF = False
        while True : # get initial hello msg
            more = self.the_conn.recv(256) # msg is small
            if len(more) == 0 :
                self.EOF = True
                break
            #end if
            more = more.decode()
            if self.debug :
                sys.stderr.write("init got more: %s\n" % repr(more))
            #end if
            self.buff += more
            if self.buff.find(self.NL) >= 0 :
                break
        #end while
        self.hello, self.buff = self.buff.split(self.NL, 1)
    #end __init__

    def fileno(self) :
        "allows use in a select, for example to check if" \
        " any unsolicited events are available to be read."
        return self.the_conn.fileno()
    #end fileno

    def close(self) :
        "closes the Asterisk Manager connection. Calling this on an" \
        " already-closed connection is harmless."
        if self.the_conn != None :
            self.the_conn.close()
            self.the_conn = None
        #end if
    #end close

#end Manager

#+
# Asterisk Gateway Interface
#-

class AGI :
    "for use by a script invoked via the AGI, DeadAGI or EAGI dialplan commands."

    def __init__(self, *, from_asterisk = None, to_asterisk = None, args = None, EAGI = False) :
        "from_asterisk and to_asterisk are file objects to use to communicate" \
        " with Asterisk; default to sys.stdin and sys.stdout if not specified, while" \
        " args are taken from sys.argv if not specified.\n" \
        "EAGI indicates whether to set audio_in attribute to a file object for" \
        " reading linear PCM audio from the channel (only possible if the script" \
        " was invoked via the EAGI application command).\n" \
        "agi_vars attribute will be set to a dictionary containing all the initial" \
        " AGI variable definitions passed from Asterisk."
        self.debug = False # can be set to True by caller
        if from_asterisk == None :
            from_asterisk = sys.stdin
        #end if
        if to_asterisk == None :
            to_asterisk = sys.stdout
        #end if
        if args != None :
            self.args = args
        else :
            self.args = sys.argv
        #end if
        self.from_asterisk = from_asterisk
        self.to_asterisk = to_asterisk
        self.audio_in = None
        if EAGI :
            self.audio_in = os.fdopen(3, "r")
        #end if
        self.agi_vars = {}
        while True :
            line = self.from_asterisk.readline().rstrip("\n")
            if len(line) == 0 :
                break
            name, value = line.split(": ", 1)
            self.agi_vars[name] = value
        #end while
    #end __init__

    def request(self, req) :
        "send a generic request line and return a 3-tuple of (code, text, rest) on success."
        if self.debug  :
            sys.stderr.write("sending request: %s\n" % repr(req))
        #end if
        self.to_asterisk.write(req + "\n")
        self.to_asterisk.flush()
        line = self.from_asterisk.readline().rstrip("\n")
        if self.debug :
            sys.stderr.write("first response line: %s\n" % repr(line))
        #end if
        if not line.startswith("200") :
            raise RuntimeError("Asterisk AGI error: %s" % line)
        #end if
        continued = line[3] == "-"
        line = line[4:]
        if not line.startswith("result=") :
            raise RuntimeError("Asterisk AGI returned unexpected result: %s" % line)
        #end if
        line = line[7:]
        line = line.split(" ", 1)
        if len(line) > 1 :
            code, text = line
            if text.startswith("(") and text.endswith(")") :
                text = text[1:-1]
            #end if
        else :
            code = line[0]
            text = None
        #end if
        rest = None
        if continued :
            # not sure if this is correct yet
            while True :
                line = self.from_asterisk.readline()
                if rest == None :
                    rest = line
                else :
                    rest += line
                #end if
                if line.startswith(code) :
                    break
            #end while
        #end if
        return (int(code), text, rest)
    #end request

    # specific functions, built on top of request
    # could implement more of those listed here <http://www.voip-info.org/wiki/view/Asterisk+AGI>

    def get_variable(self, varname) :
        "returns the value of the specified Asterisk global, or None if not defined."
        return \
            self.request("GET VARIABLE %s" % varname)[1]
    #end get_variable

#end AGI
