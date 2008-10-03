#+
# Various useful Asterisk-related definitions.
#
# Start of development 2007 March 2 by Lawrence D'Oliveiro
#    <ldo@geek-central.gen.nz>.
# Separated out from asterisk_test 2007 July 20.
# Add GotMoreResponse 2008 April 14.
# Add AutoMultiResponse 2008 August 15.
# Split response line on only first colon 2008 August 22.
# Add GetQueueStatus 2008 August 22.
# Separate out SendRequest 2008 October 3.
# Add multiresponse error checking 2008 October 3.
#-

import sys
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
