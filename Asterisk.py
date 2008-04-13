#+
# Various useful Asterisk-related definitions.
#
# Start of development 2007 March 2 by Lawrence D'Oliveiro
#    <ldo@geek-central.gen.nz>.
# Separated out from asterisk_test 2007 July 20.
# Add GotMoreResponse 2008 April 14.
#-

import sys
import socket

class Manager :
	"""simple management of an Asterisk Manager API connection."""

	NL = "\015\012" # protocol line delimiter

	def GetResponse(self) :
		"""reads and parses another response from the Asterisk Manager connection."""
		Response = {}
		while True :
			Split = self.Buff.split(self.NL, 1)
			if len(Split) == 2 :
				self.Buff = Split[1]
				if len(Split[0]) == 0 :
					break
				(Keyword, Value) = Split[0].split(": ")
				Response[Keyword] = Value
			else :
				if self.Debug :
					sys.stderr.write("Getting more\n")
				#end if
				self.Buff += self.TheConn.recv(4096)
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

	def Transact(self, Action, Parms, MultiResponse = False) :
		"""does a basic transaction and returns the single response
		or sequence of responses, depending on MultiResponse.
		MultiResponse can be a string specifying the Response
		value that indicates the end of a response sequence."""
		ToSend = "Action: " + Action + self.NL
		for Parm in Parms.keys() :
			ToSend += Parm + ": " + str(Parms[Parm]) + self.NL
		#end for
		ToSend += self.NL # marks end of request
		if self.Debug :
			sys.stderr.write(ToSend)
		#end if
		while len(ToSend) != 0 :
			Sent = self.TheConn.send(ToSend)
			ToSend = ToSend[Sent:]
		#end while
		if MultiResponse != None and MultiResponse != False :
			Response = []
			while True :
				NextResponse = self.GetResponse()
				if len(NextResponse) == 0 :
					break
				if self.Debug :
					sys.stderr.write \
					  (
						"NextResponse: \"%s\"\n" % repr(NextResponse)
					  )
				#end if
				Response.append(NextResponse)
				if \
						type(MultiResponse) == str \
					and \
						NextResponse.get("Event", None) == MultiResponse \
				:
					break
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

	def __init__(self, Host = "127.0.0.1", Port = 5038) :
		"""opens connection and receives initial Hello message
		from Asterisk."""
		self.Debug = False
		self.TheConn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.TheConn.connect((Host, Port))
		self.Buff = ""
		while True : # get initial hello msg
			self.Buff += self.TheConn.recv(256) # msg is small
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
