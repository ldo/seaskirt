#+
# Various useful Asterisk-related definitions.
#
# Created by Lawrence D'Oliveiro <ldo@geek-central.gen.nz>.
#-

import sys
import os
import socket

class Manager :
    """simple management of an Asterisk Manager API connection."""

    NL = "\015\012" # protocol line delimiter

    AutoMultiResponse = \
      { # table of actions which are automatically recognized as being MultiResponse.
        # Note keys are lowercase, while values are case-sensitive.
        "agents" : "AgentsComplete",
        "parkedcalls" : "ParkedCallsComplete",
        "queuestatus" : "QueueStatusComplete",
        "sippeers" : "PeerlistComplete",
        "status" : "StatusComplete",
        "zapshowchannels" : "ZapShowChannelsComplete",
      }

    @classmethod
    def Sanitize(self, Parm) :
        # sanitizes the value of Parm to avoid misbehaviour with Manager API syntax.
        return str(Parm).replace("\n", "")
    #end Sanitize

    def SendRequest(self, Action, Parms, Vars = None) :
        """sends a request to the Manager."""
        ToSend = "Action: " + Action + self.NL
        for Parm in Parms.keys() :
            ToSend += Parm + ": " + self.Sanitize(Parms[Parm]) + self.NL
        #end for
        if Vars != None :
            for Var in Vars.keys() :
                ToSend += \
                    "Variable: " + self.Sanitize(Var) + "=" + self.Sanitize(Vars[Var]) + self.NL
            #end for
        #end if
        ToSend += self.NL # marks end of request
        if self.Debug :
            sys.stderr.write(ToSend)
        #end if
        while len(ToSend) != 0 :
            Sent = self.TheConn.send(ToSend)
            ToSend = ToSend[Sent:]
        #end while
    #end SendRequest

    def GetResponse(self) :
        """reads and parses another response from the Asterisk Manager connection."""
        Response = {}
        while True :
            Split = self.Buff.split(self.NL, 1)
            if len(Split) == 2 :
                self.Buff = Split[1]
                if len(Split[0]) == 0 :
                    break
                (Keyword, Value) = Split[0].split(": ", 1)
                Response[Keyword] = Value
            else :
                if self.Debug :
                    sys.stderr.write("Getting more\n")
                #end if
                More = self.TheConn.recv(4096)
                if len(More) == 0 :
                    self.EOF = True
                    break
                #end if
                self.Buff += More
                if self.Debug :
                    sys.stderr.write \
                      (
                        "Got (%u): \"%s\"\n" % (len(self.Buff), self.Buff)
                      )
                #end if
            #end if
        #end while
        return Response
    #end GetResponse

    def GotMoreResponse(self) :
        """returns True iff there's another response from the Asterisk Manager
        connection in the buffer waiting to be parsed and returned."""
        return len(self.Buff.split(self.NL + self.NL, 1)) == 2
    #end GotMoreResponse

    def Transact(self, Action, Parms, Vars = None) :
        """does a basic transaction and returns the single response
        or sequence of responses. Note this doesn't currently handle
        commands like "IAXpeers" or "Queues" that don't return
        response lines in the usual "keyword: value" format."""
        self.SendRequest(Action, Parms, Vars)
        MultiResponse = self.AutoMultiResponse.get(Action.lower())
        if MultiResponse != None :
            Response = []
            FirstResponse = True
            while True :
                NextResponse = self.GetResponse()
                if self.EOF or len(NextResponse) == 0 :
                    break
                if self.Debug :
                    sys.stderr.write \
                      (
                        "NextResponse: \"%s\"\n" % repr(NextResponse)
                      )
                #end if
                if FirstResponse :
                    # check for success/failure
                    if not NextResponse.get("Response", None) == "Success" :
                        raise RuntimeError \
                          (
                            "%s failed -- %s" % (Action, NextResponse.get("Message", "?"))
                          )
                    #end if
                    FirstResponse = False
                else :
                    Response.append(NextResponse)
                    if (
                            type(MultiResponse) == str
                        and
                            NextResponse.get("Event", None) == MultiResponse
                    ) :
                        break
                #end if
            #end while
        else :
            Response = self.GetResponse()
        #end if
        return Response
    #end Transact

    def Authenticate(self, Username, Password, WantEvents = False) :
        """logs in with a username and password. This is mandatory
        after opening the connection, before trying any other
        commands. WantEvents indicates whether you want to receive
        unsolicited event notifications on this connection."""
        Parms = \
            {
                "Username" : Username,
                "Secret" : Password,
            }
        if not WantEvents :
            Parms["Events"] = "off"
        #end if
        Response = self.Transact \
          (
            Action = "Login",
            Parms = Parms
          )
        if Response["Response"] != "Success" :
            raise RuntimeError("authentication failed")
        #end if
    #end Authenticate

    def DoCommand(self, Command) :
        """does a Command request and returns the response text."""
        self.SendRequest("Command", {"Command" : Command})
        Response = ""
        FirstResponse = True
        Status = None
        while True :
            while True :
                if self.Buff.find(self.NL) >= 0 or self.EOF :
                    break
                if self.Debug :
                    sys.stderr.write("Getting more\n")
                #end if
                More = self.TheConn.recv(4096)
                if len(More) == 0 :
                    self.EOF = True
                    break
                #end if
                self.Buff += More
                if self.Debug :
                    sys.stderr.write \
                      (
                        "Got (%u): \"%s\"\n" % (len(self.Buff), self.Buff)
                      )
                #end if
            #end while
            if self.Buff.find(self.NL) < 0 :
                break
            if FirstResponse :
                Line, self.Buff = self.Buff.split(self.NL, 1)
                Items = Line.split(": ", 1)
                if len(Items) == 2 :
                    if Items[0] == "Response" :
                        Status = Items[1]
                        if Status != "Follows" :
                            raise RuntimeError \
                              (
                                "Command failed -- %s" % (Status,)
                              )
                        #end if
                    #end if
                else :
                    FirstResponse = False
                    self.Buff = Line + self.NL + self.Buff
                    if Status == None :
                        raise RuntimeError("No Response received for Command")
                    #end if
                #end if
            #end if
            if not FirstResponse :
                Items = self.Buff.split("--END COMMAND--" + self.NL + self.NL, 1)
                if len(Items) == 2 :
                    Response = Items[0]
                    self.Buff = Items[1]
                    break
                #end if
            #end if
        #end while
        return Response
    #end DoCommand

    def GetQueueStatus(self) :
        """does a QueueStatus request and returns the parsed response as a list
        of entries, one per queue."""
        Response = self.Transact("QueueStatus", {})
        Result = {}
        Responses = iter(Response)
        LastQueue = None # to begin with
        while True :
            try :
                ResponseItem = Responses.next()
            except StopIteration :
                ResponseItem = None
            #end try
            if ResponseItem != None :
                Kind = ResponseItem.get("Event") # absent for first response item
            else :
                Kind = "QueueParams" # dummy to finish entry for last queue
            #end if
            if Kind == "QueueParams" :
                if LastQueue != None :
                    LastQueue["members"] = LastQueueMembers
                    LastQueue["entries"] = LastQueueEntries
                    Result[LastQueueName] = LastQueue
                #end if
                if ResponseItem == None :
                    break
                LastQueueName = ResponseItem["Queue"]
                LastQueue = dict(ResponseItem)
                LastQueueMembers = []
                LastQueueEntries = []
            elif Kind == "QueueMember" :
                LastQueueMembers.append(dict(ResponseItem))
            elif Kind == "QueueEntry" :
                LastQueueEntries.append(dict(ResponseItem))
            #end if
        #end while
        return Result
    #end GetQueueStatus

    def GetChannels(self) :
        """gets information on all currently-existing channels."""
        Result = []
        Fields = \
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
        for Line in self.DoCommand("core show channels concise").split("\012") :
            Line = Line.split("!")
            if len(Line) >= len(Fields) :
                Result.append(dict(zip(Fields, Line)))
            #end if
        #end for
        return Result
    #end GetChannels

    def __init__(self, Host = "127.0.0.1", Port = 5038) :
        """opens connection and receives initial Hello message
        from Asterisk."""
        self.Debug = False # can be set to True by caller
        self.TheConn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.TheConn.connect((Host, Port))
        self.Buff = ""
        self.EOF = False
        while True : # get initial hello msg
            More = self.TheConn.recv(256) # msg is small
            if len(More) == 0 :
                self.EOF = True
                break
            #end if
            self.Buff += More
            if self.Buff.find(self.NL) >= 0 :
                break
        #end while
        (self.Hello, self.Buff) = self.Buff.split(self.NL, 1)
    #end __init__

    def fileno(self) :
        """allows use in a select, for example to check if
        any unsolicited events are available to be read."""
        return self.TheConn.fileno()
    #end fileno

    def close(self) :
        """closes the Asterisk Manager connection."""
        self.TheConn.close()
    #end close

#end Manager

class AGI :
    """for use by a script invoked via the AGI, DeadAGI or EAGI dialplan commands."""

    def __init__(self, from_asterisk = None, to_asterisk = None, args = None, EAGI = False) :
        """from_asterisk and to_asterisk are file objects to use to communicate
        with Asterisk; default to sys.stdin and sys.stdout if not specified, while
        args are taken from sys.argv if not specified.
        EAGI indicates whether to set audio_in attribute to a file object for
        reading linear PCM audio from the channel (only possible if the script
        was invoked via the EAGI application command).
        agi_vars attribute will be set to a dictionary containing all the initial
        AGI variable definitions passed from Asterisk."""
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
        """send a generic request line and return a 3-tuple of (code, text, rest) on success."""
        sys.stderr.write("sending request: %s\n" % repr(req)) # debug
        sys.stderr.flush() # debug
        self.to_asterisk.write(req + "\n")
        self.to_asterisk.flush()
        line = self.from_asterisk.readline().rstrip("\n")
        sys.stderr.write("first response line: %s\n" % repr(line)) # debug
        if not line.startswith("200") :
            raise RuntimeError("Asterisk AGI error: %s" % line)
        #end if
        sys.stderr.flush() # debug
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

    def get_variable(self, varname) :
        """returns the value of the specified Asterisk global, or None if not defined."""
        return \
            self.request("GET VARIABLE %s" % varname)[1]
    #end get_variable

#end AGI
