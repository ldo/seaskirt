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
import enum
import errno
from weakref import \
    ref as weak_ref
import socket
import threading
import queue
import base64
import urllib.parse
import urllib.request
import json
import inspect
import ast
import select
import asyncio
import wsproto
import wsproto.events as wsevents

#+
# Macro preprocessor
#
# This mechanism allows for synchronous and asynchronous versions of
# classes to share common code. Methods that come in both forms will
# look something like
#
#     async def «method» ...
#         ...
#         if ASYNC :
#             «asynchronous variant»
#         else :
#             «synchronous variant»
#         #end if
#         ...
#     #end «method»
#
# where “ASYNC” is an undefined variable, used as a special condition
# marker at every point where the two variants diverge.
#
# The abstract syntax tree for this code will be processed into two
# variants, that (if they were decompiled to source form) would look
# like
#
#     def «method» ...
#         ...
#         «synchronous variant»
#         ...
#     #end «method»
#
#     async def «method» ...
#         ...
#         «asynchronous variant»
#         ...
#     #end «method»
#
# Notice that all references to “ASYNC” disappear, leaving the
# synchronous version executing unconditionally synchronous code,
# and the asynchronous version unconditionally using the asynchronous
# variants of that code.
#
# However, note that __init__ is not allowed to be async def
# (must return None, not a coroutine), so if you want to do
# asynchronous object creation, you need to do object construction
# in a __new__ method instead.
#-

class ConditionalExpander(ast.NodeTransformer) :
    "generates synchronous or asynchronous variants of a class from common code."

    def __init__(self, classname, newclassname, is_async) :
        self.classname = classname
        self.newclassname = newclassname
        self.is_async = is_async
    #end __init__

    def visit_ClassDef(self, node) :
        body = list(self.visit(b) for b in node.body)
        result = node
        if result.name == self.classname :
            # any inner classes keep the same name in both versions
            result.name = self.newclassname
        #end if
        result.body = body
        return \
            result
    #end visit_ClassDef

    def visit_AsyncFunctionDef(self, node) :
        body = list(self.visit(b) for b in node.body)
        if self.is_async :
            result = node
            result.body = body
        else :
            result = ast.FunctionDef \
              (
                name = node.name,
                args = node.args,
                body = body,
                decorator_list = node.decorator_list,
                returns = node.returns,
                type_comment = node.type_comment
              )
        #end if
        return \
            result
    #end visit_AsyncFunctionDef

    def visit_If(self, node) :
        result = None
        if isinstance(node.test, ast.Name) :
            if node.test.id == "ASYNC" and isinstance(node.test.ctx, ast.Load) :
                if self.is_async :
                    if len(node.body) > 1 :
                        result = ast.If \
                          (
                            test = ast.Constant(True),
                            body = node.body,
                            orelse = []
                          )
                    elif len(node.body) == 1 :
                        result = node.body[0]
                    else :
                        result = ast.Pass()
                    #end if
                else :
                    if len(node.orelse) > 1 :
                        result = ast.If \
                          (
                            test = ast.Constant(True),
                            body = node.orelse,
                            orelse = []
                          )
                    elif len(node.orelse) == 1 :
                        result = node.orelse[0]
                    else :
                        result = ast.Pass()
                    #end if
                #end if
            #end if
        #end if
        if result == None :
            result = ast.If \
              (
                test = node.test,
                body = list(self.visit(b) for b in node.body),
                orelse = list(self.visit(b) for b in node.orelse),
              )
        #end if
        return \
            result
    #end visit_If

#end ConditionalExpander

def def_sync_async_classes(class_template, sync_classname, async_classname) :
    "takes the class object class_template and generates the synchronous and" \
    " asynchronous forms of the class, defining them in this module’s global" \
    " namespace where the former is named sync_classname and the latter is named" \
    " async_classname."
    src = inspect.getsource(class_template)
    syntax = ast.parse(src, mode = "exec")
    sync_version = ConditionalExpander \
      (
        classname = class_template.__name__,
        newclassname = sync_classname,
        is_async = False
      ).visit(syntax)
    syntax = ast.parse(src, mode = "exec")
    async_version = ConditionalExpander \
      (
        classname = class_template.__name__,
        newclassname = async_classname,
        is_async = True
      ).visit(syntax)
    ast.fix_missing_locations(sync_version)
    ast.fix_missing_locations(async_version)
    exec(compile(sync_version, filename = __file__, mode = "exec"), globals())
    exec(compile(async_version, filename = __file__, mode = "exec"), globals())
#end def_sync_async_classes

#+
# Useful stuff
#-

def call_async(func, funcargs = (), timeout = None, abort = None, loop = None) :
    "invokes func on a separate temporary thread and returns a Future that" \
    " can be used to wait for its completion and obtain its result. If timeout" \
    " is not None, then waiters on the Future will get a TimeoutError exception" \
    " if the function has not completed execution after that number of seconds." \
    " This allows easy invocation of blocking I/O functions in an asyncio-" \
    "compatible fashion. But note that the operation cannot be cancelled" \
    " if the timeout elapses; instead, you can specify an abort callback" \
    " which will be invoked with whatever result is eventually returned from" \
    " func."

    if loop == None :
        loop = asyncio.get_running_loop()
    #end if

    timeout_task = None

    def func_done(ref_awaiting, result, exc) :
        awaiting = ref_awaiting()
        if awaiting != None :
            if not awaiting.done() :
                if exc != None :
                    awaiting.set_exception(exc)
                else :
                    awaiting.set_result(result)
                #end if
                if timeout_task != None :
                    timeout_task.cancel()
                #end if
            else :
                if abort != None :
                    abort(result)
                #end if
            #end if
        #end if
    #end func_done

    def do_func_timedout(ref_awaiting) :
        awaiting = ref_awaiting()
        if awaiting != None :
            if not awaiting.done() :
                awaiting.set_exception(TimeoutError())
                # Python doesn’t give me any (easy) way to cancel the thread running the
                # do_func() call, so just let it run to completion, whereupon func_done()
                # will get rid of the result. Even if I could delete the thread, can I be sure
                # that would clean up memory and OS/library resources properly?
            #end if
        #end if
    #end do_func_timedout

    def do_func(ref_awaiting) :
        # makes the blocking call on a separate thread.
        fail = None
        try :
            result = func(*funcargs)
        except Exception as err :
            fail = err
            result = None
        #end try
        # A Future is not itself threadsafe, but I can thread-safely
        # run a callback on the main thread to set it.
        loop.call_soon_threadsafe(func_done, ref_awaiting, result, fail)
    #end do_func

#begin call_async
    awaiting = loop.create_future()
    ref_awaiting = weak_ref(awaiting)
      # weak ref to avoid circular refs with loop
    subthread = threading.Thread(target = do_func, args = (ref_awaiting,), daemon = True)
    subthread.start()
    if timeout != None :
        timeout_task = loop.call_later(timeout, do_func_timedout, ref_awaiting)
    #end if
    return \
        awaiting
#end call_async

class AsyncFile :
    "wrapper around Python file objects which makes all (relevant) calls" \
    " asynchronous by passing them off to a dedicated request-runner thread."

    def __init__(self, fv) :
        self.fv = fv
        self.requests = queue.Queue(maxsize = 1)
        self.runner = threading.Thread \
          (
            target = self.do_run,
            args = (self.requests,),
            daemon = True
          )
        self.runner.start()
    #end __init__

    def fileno(self) :
        return \
            self.fv.fileno()
    #end fileno

    class Request :
        "represents an I/O request to be executed by the request-runner" \
        " thread, and includes a future so completion (or failure) can" \
        " be reported back to the initiating thread."

        def __init__(self, parent, fn, fnargs) :
            # fills out the Request and puts it on the thread queue.
            self.loop = asyncio.get_running_loop()
            self.fn = fn
            self.fnargs = fnargs
            self.notify_done = self.loop.create_future()
            parent.requests.put(self)
        #end __init__

        def __await__(self) :
            return \
                self.notify_done.__await__()
        #end __await__

        def request_done(self, res, fail) :
            # queued back to the original thread by the thread runner
            # to return result or raise exception via the notify_done future.
            if fail != None :
                self.notify_done.set_exception(fail)
            else :
                self.notify_done.set_result(res)
            #end if
        #end request_done

    #end Request

    @staticmethod
    def do_run(requests) :
        # processes actual I/O requests on a separate thread.
        # Note I don’t have an explicit procedure for shutting
        # this down (e.g. when the file is closed), because I’m
        # not expecting many of these objects to be created
        # and destroyed over the life of the process.
        while True :
            elt = requests.get()
            fail = None
            try :
                res = elt.fn(*elt.fnargs)
            except Exception as err :
                fail = err
                res = None
            #end try
            elt.loop.call_soon_threadsafe(elt.request_done, res, fail)
            requests.task_done()
        #end while
    #end do_run

    # async wrappers around synchronous file calls -- list all the ones I actually use

    async def read(self, nrbytes) :
        return \
            await type(self).Request(self, self.fv.read, (nrbytes,))
    #end read

    async def readline(self) :
        return \
            await type(self).Request(self, self.fv.readline, ())
    #end readline

    async def write(self, data) :
        return \
            await type(self).Request(self, self.fv.write, (data,))
    #end write

    async def flush(self) :
        return \
            await type(self).Request(self, self.fv.flush, ())
    #end flush

    async def close(self) :
        return \
            await type(self).Request(self, self.fv.close, ())
    #end close

#end AsyncFile

IOBUFSIZE = 4096 # size of most I/O buffers
SMALL_IOBUFSIZE = 256 # size used for reads expected to be small

def naturals() :
    "returns the sequence of natural numbers. May be used as a" \
    " unique-id generator."
    i = 0
    while True :
        i += 1
        yield str(i)
    #end while
#end naturals

def quote_url(s) :
    return \
        urllib.parse.quote(s, safe = "")
#end quote_url

def sock_wait(sock, recv, send, timeout = None) :
    "waits until the specified socket has something to be received or sent," \
    " or the timeout (if specified) elapses."
    if not (recv or send) :
        raise RuntimeError("need to wait for either receive or send or both")
    #end if
    poll = select.poll()
    poll.register \
      (
        sock,
        (0, select.POLLOUT)[send] | (0, select.POLLIN)[recv]
      )
    ready = poll.poll \
      (
        (lambda : None, lambda : round(timeout * 1000))
            [timeout != None]()
      )
    if len(ready) != 0 :
        ready = ready[0]
        assert ready[0] == sock.fileno()
        flags = ready[1]
        receiving = flags & select.POLLIN != 0
        sending = flags & select.POLLOUT != 0
    else :
        receiving = sending = False
    #end if
    return \
        (receiving, sending)
#end sock_wait

async def sock_wait_async(sock, recv, send, timeout = None) :
    "waits until the specified socket has something to be received or sent," \
    " or the timeout (if specified) elapses."
    if not (recv or send) :
        raise RuntimeError("need to wait for either receive or send or both")
    #end if
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    flags = 0

    def fd_ready(writing) :
        nonlocal flags
        flags |= (select.POLLIN, select.POLLOUT)[writing]
        if not ready.done() :
            ready.set_result(False)
        #end if
    #end fd_ready

    def fd_timeout() :
        if not ready.done() :
            ready.set_result(True)
        #end if
    #end fd_timeout

    timeout_task = None
    if recv :
        loop.add_reader(sock, fd_ready, False)
    #end if
    if send :
        loop.add_writer(sock, fd_ready, True)
    #end if
    if timeout != None :
        timeout_task = loop.call_later(timeout, fd_timeout)
    #end if
    timed_out = await ready
    if recv :
        loop.remove_reader(sock)
    #end if
    if send :
        loop.remove_writer(sock)
    #end if
    if timeout_task != None :
        timeout_task.cancel()
    #end if
    if timed_out :
        receiving = sending = False
    else :
        receiving = select.POLLIN & flags != 0
        sending = select.POLLOUT & flags != 0
    #end if
    return \
        (receiving, sending)
#end sock_wait_async

#+
# Asterisk Manager Interface
#-

class Manager :
    "simple management of an Asterisk Manager API connection. When setting" \
    " up the connection, you specify whether you want to receive asynchronous" \
    " (unsolicited) events. It is a good idea, where possible, to turn this on" \
    " only on a connection which is not being used to send action requests to" \
    " Asterisk; if you need both, open a separate connection for action requests" \
    " if you can, with unsolicited event reception turned off.\n" \
    "\n" \
    "However, there is a difficulty with this if you want to use the async" \
    " mode of the Originate action. Because in this mode the corresponding" \
    " OriginateResponse is sent as an event, but only on the connection" \
    " which initiated the call, and only if unsolicited event reception is" \
    " enabled on that connection.\n" \
    "\n" \
    "So, in this situation, you need to be able to pair up the response event" \
    " with its corresponding action request, ignoring other events which might" \
    " appear in the meantime. To achieve this, specify an iterator that returns some" \
    " endless sequence of non-repeating strings as the id_gen arg; this will ensure" \
    " that every request goes out with a unique ActionID value, which will be returned" \
    " in the corresponding response. You can use the naturals() function for this" \
    " purpose."

    NL = "\015\012" # protocol line delimiter

    @classmethod
    def sanitize(celf, parm) :
        # sanitizes the value of parm to avoid misbehaviour with Manager API syntax.
        return str(parm).replace("\n", "")
    #end sanitize

    async def __new__(celf, host = "127.0.0.1", port = 5038, *, username, password, want_events = False, id_gen = None, timeout = None, debug = False) :
        "opens connection, receives initial Hello message from Asterisk, and does" \
        " initial mandatory authentication handshake."
        self = super().__new__(celf)
        self.debug = debug
        self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if id_gen != None and not hasattr(id_gen, "__next__") :
            raise TypeError("id_gen is not an iterator")
        #end if
        self.id_gen = id_gen
        self.last_request_id = None
        if timeout != None :
            self.conn.settimeout(timeout)
        #end if
        if ASYNC :
            await call_async(self.conn.connect, ((host, port),))
            self.conn.setblocking(False)
        else :
            self.conn.connect((host, port))
        #end if
        self.buff = ""
        self.EOF = False
        while True : # get initial hello msg
            if ASYNC :
                more = await asyncio.get_running_loop().sock_recv(self.conn, SMALL_IOBUFSIZE)
            else :
                more = self.conn.recv(SMALL_IOBUFSIZE)
            #end if
            if len(more) == 0 :
                self.EOF = True
                break
            #end if
            more = more.decode()
            if self.debug :
                sys.stderr.write("Manager init got more: %s\n" % repr(more))
            #end if
            self.buff += more
            if self.buff.find(self.NL) >= 0 :
                break
        #end while
        self.hello, self.buff = self.buff.split(self.NL, 1)
        parms = \
            {
                "Username" : username,
                "Secret" : password,
            }
        if not want_events :
            parms["Events"] = "off"
        #end if
        if ASYNC :
            response = await self.transact(action = "Login", parms = parms)
        else :
            response = self.transact(action = "Login", parms = parms)
        #end if
        if response["Response"] != "Success" :
            raise RuntimeError("authentication failed")
        #end if
        return \
            self
    #end __new__

    async def close(self) :
        "closes the Asterisk Manager connection. Calling this on an" \
        " already-closed connection is harmless."
        if self.conn != None :
            if ASYNC :
                await call_async(self.conn.close, ())
            else :
                self.conn.close()
            #end if
            self.conn = None
        #end if
    #end close

    def fileno(self) :
        "allows use in a select, for example to check if" \
        " any unsolicited events are available to be read."
        return self.conn.fileno()
    #end fileno

    async def send_request(self, action, parms, vars = None) :
        "sends a request to the Manager, leaving it up to you to retrieve" \
        " any subsequent response with get_response."
        to_send = "Action: " + action + self.NL
        if self.id_gen != None :
            self.last_request_id = next(self.id_gen)
            to_send += "ActionID: %s%s" % (self.last_request_id, self.NL)
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
            sys.stderr.write("Manager sending: %s" % to_send)
        #end if
        if ASYNC :
            await asyncio.get_running_loop().sock_sendall(self.conn, to_send.encode())
        else :
            self.conn.sendall(to_send.encode())
        #end if
    #end send_request

    async def get_response(self) :
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
                keyword, value = split[0].split(": ", 1)
                if keyword in response :
                    response[keyword] += "\n" + value
                else :
                    response[keyword] = value
                #end if
            else :
                if self.debug :
                    sys.stderr.write("Manager getting more\n")
                #end if
                if ASYNC :
                    recv = (await sock_wait_async(self.conn, True, False))[0]
                else :
                    recv = sock_wait(self.conn, True, False)[0]
                #end if
                assert recv
                more = self.conn.recv(IOBUFSIZE)
                if len(more) == 0 :
                    self.EOF = True
                    break
                #end if
                self.buff += more.decode()
                if self.debug :
                    sys.stderr.write \
                      (
                        "Manager got (%u): \"%s\"\n" % (len(self.buff), self.buff)
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

    async def transact(self, action, parms, vars = None) :
        "does a basic transaction and returns the single response" \
        " or sequence of responses."
        if ASYNC :
            await self.send_request(action, parms, vars)
        else :
            self.send_request(action, parms, vars)
        #end if
        response = []
        multi_response = False # to begin with
        first_response = True
        while True :
            if ASYNC :
                next_response = await self.get_response()
            else :
                next_response = self.get_response()
            #end if
            if self.EOF or len(next_response) == 0 :
                break
            if self.debug :
                sys.stderr.write \
                  (
                    "Manager next_response: \"%s\"\n" % repr(next_response)
                  )
            #end if
            if self.last_request_id == None or next_response.get("ActionID") == self.last_request_id :
                if first_response :
                    # check for success/failure
                    if next_response.get("Response") not in ("Success", "Goodbye") :
                        raise RuntimeError \
                          (
                            "%s failed -- %s" % (action, next_response.get("Message", "?"))
                          )
                    #end if
                    first_response = False
                    if next_response.get("EventList") == "start" :
                        multi_response = True
                    #end if
                #end if
                response.append(next_response)
                if not multi_response :
                    break
                if next_response.get("EventList") == "Complete" :
                    break
            #end if
        #end while
        if not multi_response :
            assert len(response) == 1
            response, = response
        #end if
        return response
    #end transact

    async def do_command(self, command) :
        "does a Command request and returns the response text."
        if ASYNC :
            await self.send_request("Command", {"Command" : command})
        else :
            self.send_request("Command", {"Command" : command})
        #end if
        response = ""
        first_response = True
        status = None
        while True :
            while True :
                if self.buff.find(self.NL) >= 0 or self.EOF :
                    break
                if self.debug :
                    sys.stderr.write("Manager command getting more\n")
                #end if
                if ASYNC :
                    more = await asyncio.get_running_loop().sock_recv(self.conn, IOBUFSIZE)
                else :
                    more = self.conn.recv(IOBUFSIZE)
                #end if
                if len(more) == 0 :
                    self.EOF = True
                    break
                #end if
                self.buff += more.decode()
                if self.debug :
                    sys.stderr.write \
                      (
                        "Manager command got (%u): \"%s\"\n" % (len(self.buff), self.buff)
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

    async def get_queue_status(self) :
        "does a QueueStatus request and returns the parsed response as a list" \
        " of entries, one per queue."
        if ASYNC :
            response = await self.transact("QueueStatus", {})
        else :
            response = self.transact("QueueStatus", {})
        #end if
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

    async def get_channels(self) :
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
        if ASYNC :
            response = await self.do_command("core show channels concise").split("\012")
        else :
            response = self.do_command("core show channels concise").split("\012")
        #end if
        for line in response :
            line = line.split("!")
            if len(line) >= len(fields) :
                result.append(dict(zip(fields, line)))
            #end if
        #end for
        return result
    #end get_channels

#end Manager

def_sync_async_classes(Manager, "Manager", "ManagerAsync")

#+
# Asterisk Gateway Interface
#-

class Gateway :
    "for use by a script invoked via the AGI, EAGI or FastAGI dialplan commands." \
    " For FastAGI use, you can call the listener() classmethod, which returns an" \
    " instance of the Listener inner class, to listen on a particular port for" \
    " incoming connections; the accept() method of this class will return a Gateway" \
    " instance for each such connection."

    async def __new__(celf, *, from_asterisk = None, to_asterisk = None, args = None, with_audio_in = False) :
        "from_asterisk and to_asterisk are file objects to use to communicate" \
        " with Asterisk; default to sys.stdin and sys.stdout if not specified, while" \
        " args are taken from sys.argv if not specified.\n" \
        "with_audio_in indicates whether to set audio_in attribute to a file object for" \
        " reading linear PCM audio from the channel (only possible if the script" \
        " was invoked via the EAGI application command).\n" \
        "agi_vars attribute will be set to a dictionary containing all the initial" \
        " AGI variable definitions passed from Asterisk."
        self = super().__new__(celf)
        self.debug = False # can be set to True by caller
        self.hungup = False
        if from_asterisk == None :
            from_asterisk = sys.stdin
        #end if
        if to_asterisk == None :
            to_asterisk = sys.stdout
        #end if
        if ASYNC :
            from_asterisk = AsyncFile(from_asterisk)
            to_asterisk = AsyncFile(to_asterisk)
        #end if
        if args != None :
            self.args = args
        else :
            self.args = sys.argv
        #end if
        self.from_asterisk = from_asterisk
        self.to_asterisk = to_asterisk
        self.audio_in = None
        if with_audio_in :
            try :
                self.audio_in = os.fdopen(3, "rb")
            except OSError as err :
                if err.errno != errno.EBADF :
                    raise
                #end if
                self.audio_in = None
            #end if
            if self.audio_in == None :
                raise RuntimeError("no audio-in fd available")
            #end if
            if ASYNC :
                self.audio_in = AsyncFile(self.audio_in)
            #end if
        #end if
        self.agi_vars = {}
        while True :
            if ASYNC :
                line = (await self.from_asterisk.readline()).rstrip("\n")
            else :
                line = self.from_asterisk.readline().rstrip("\n")
            #end if
            if len(line) == 0 :
                break
            if self.debug :
                sys.stderr.write("gateway var def = %s\n" % repr(line))
            #end if
            name, value = line.split(": ", 1)
            self.agi_vars[name] = value
        #end while
        return \
            self
    #end __new__

    class Listener :

        def __init__(self, parent, bindaddr, port, maxlisten = 0, debug = False) :
            self.debug = debug
            self.parent = parent
            self.conn = socket.socket()
            self.conn.bind((bindaddr, port))
            self.conn.listen(maxlisten)
        #end __init__

        def fileno(self) :
            return \
                self.conn.fileno()
        #end fileno

        async def accept(self) :
            if ASYNC :
                sock, peer = await call_async(self.conn.accept, ())
            else :
                sock, peer = self.conn.accept()
            #end if
            connin = os.fdopen(os.dup(sock.fileno()), "rt")
            connout = os.fdopen(os.dup(sock.fileno()), "wt")
            if self.debug :
                sys.stderr.write("%s.Listener connection from %s\n" % (self.parent.__name__, peer))
            #end if
            if ASYNC :
                result = await self.parent(from_asterisk = connin, to_asterisk = connout)
            else :
                result = self.parent(from_asterisk = connin, to_asterisk = connout)
            #end if
            result.debug = self.debug
            return \
                result
        #end listen

        async def close(self) :
            if self.conn != None :
                if ASYNC :
                    await call_async(self.conn.close, ())
                else :
                    self.conn.close()
                #end if
                self.conn = None
            #end if
        #end close

    #end Listener

    @classmethod
    def listener(celf, bindaddr, port, maxlisten = 0, debug = False) :
        return \
            celf.Listener(celf, bindaddr, port, maxlisten, debug)
    #end listener

    async def close(self) :
        if self.from_asterisk != None :
            if ASYNC :
                await self.from_asterisk.close()
            else :
                self.from_asterisk.close()
            #end if
            self.from_asterisk = None
        #end if
        if self.to_asterisk != None :
            if ASYNC :
                await self.to_asterisk.close()
            else :
                self.to_asterisk.close()
            #end if
            self.to_asterisk = None
        #end if
    #end close

    async def request(self, req) :
        "send a generic request line and return a 3-tuple of (code, text, rest) on success."
        if self.debug  :
            sys.stderr.write("sending request: %s\n" % repr(req))
        #end if
        if ASYNC :
            await self.to_asterisk.write(req + "\n")
            await self.to_asterisk.flush()
        else :
            self.to_asterisk.write(req + "\n")
            self.to_asterisk.flush()
        #end if
        while True :
            if ASYNC :
                line = (await self.from_asterisk.readline()).rstrip("\n")
            else :
                line = self.from_asterisk.readline().rstrip("\n")
            #end if
            if self.debug :
                sys.stderr.write("first response line: %s\n" % repr(line))
            #end if
            # HANGUP notification line can be returned for FastAGI only
            if not line.startswith("HANGUP") :
                if not line.startswith("200") :
                    raise RuntimeError("Asterisk AGI error: %s" % line)
                #end if
                break
            #end if
            self.hungup = True # and look for more response
        #end while
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
                if ASYNC :
                    line = await self.from_asterisk.readline()
                else :
                    line = self.from_asterisk.readline()
                #end if
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

    async def get_variable(self, varname) :
        "returns the value of the specified Asterisk global, or None if not defined."
        if ASYNC :
            response = await self.request("GET VARIABLE %s" % varname)
        else :
            response = self.request("GET VARIABLE %s" % varname)
        #end if
        return \
            response[1]
    #end get_variable

#end Gateway

def_sync_async_classes(Gateway, "Gateway", "GatewayAsync")

#+
# Asterisk RESTful Interface
#-

class ARIError(Exception) :
    "just to identify HTTP error codes returned from Asterisk ARI."

    def __init__(self, code, reason, headers) :
        self.code = code
        self.reason = reason
        self.headers = headers
    #end __init__

    def __str__(self) :
        return \
            "ARI Error %d: %s" % (self.code, self.reason)
    #end __str__

#end ARIError

class RESTMETHOD(enum.Enum) :
    "recognized HTTP methods used for ARI."

    # methodstr, changes_state
    DELETE = ("DELETE", True)
    GET = ("GET", False)
    POST = ("POST", True)
    PUT = ("PUT", True)

    @property
    def methodstr(self) :
        "the HTTP method string."
        return \
            self.value[0]
    #end methodstr

    @property
    def changes_state(self) :
        "whether this method changes state on the server."
        return \
            self.value[1]
    #end changes_state

#end RESTMETHOD

class ARIPasswordHandler(urllib.request.BaseHandler) :
    "password handler which only holds a single username/password pair" \
    " for the expected Asterisk realm."

    def __init__(self, username, password) :
        self.realm = "Asterisk REST Interface"
        self.username = username
        self.password = password
        self.authenticated = False
    #end __init__

    def add_password(self, realm, uri, user, passwd, is_authenticated = False) :
        raise NotImplementedError("cannot add new passwords")
    #end add_password

    def find_user_password(self, realm, authuri) :
        if realm == self.realm :
            result = (self.username, self.password)
        else :
            result = (None, None)
        #end if
        return \
            result
    #end find_user_password

    # As far as I can tell, the urllib.request code is supposed to
    # notice the existence of the following methods, and automatically
    # generate an “Authorization” header line on the initial request,
    # instead of waiting for a “401 Unauthorized” response before
    # trying again. But looking at the traffic, this doesn’t seem to
    # work.

    def update_authenticated(self, uri, is_authenticated) :
        self.authenticated = is_authenticated
    #end update_authenticated

    def is_authenticated(self, authuri) :
        return \
            self.authenticated
    #end is_authenticated

    def make_basic_auth(self) :
        "generate the contents of the “Authorization” header line myself."
        return \
            (
                "Basic "
            +
                base64.b64encode
                  (
                    ("%s:%s" % (self.username, self.password))
                        .encode()
                  ).decode()
            )
    #end make_basic_auth

#end ARIPasswordHandler

class Stasis :
    "ARI protocol wrapper. Note that a new connection is made for every call to" \
    " the request() method. Use the listen() method to create a WebSocket client" \
    " connection to listen for application events."

    async def __new__(celf, host = "127.0.0.1", port = 8088, *, prefix = "/ari", username, password, debug = False) :
        # doesn’t actually need to be async, defined async just to
        # be consistent with other main API classes.
        if prefix != "" and not prefix.startswith("/") :
            raise ValueError("nonempty prefix must start with “/”")
        #end if
        self = super().__new__(celf)
        self.host = host
        self.port = port
        self.prefix = prefix
        self.debug = debug
        self.url_base = "http://%s:%d" % (self.host, self.port)
        self.passwd = ARIPasswordHandler(username, password)
        auth = urllib.request.HTTPBasicAuthHandler(self.passwd)
        self.opener = urllib.request.build_opener(auth)
        return \
            self
    #end __init__

    async def request(self, method, path, params, data = None) :
        "initiates a request to the specified path with the specified params and" \
        "(optional) request body object, and returns a Python object decoded from" \
        " the JSON response string."
        if not isinstance(method, RESTMETHOD) :
            raise TypeError("method must be an instance of RESTMETHOD")
        #end if
        if not path.startswith("/") :
            raise ValueError("path must start with “/”")
        #end if
        if params != None :
            if (
                    isinstance(params, dict)
                and
                    all(isinstance(k, str) and isinstance(v, (int, str)) for k, v in params.items())
            ) :
                paramsstr = "&".join \
                  (
                    "%s=%s" % (k, quote_url(str(v))) for k, v in params.items()
                  )
            elif (
                    isinstance(params, tuple)
                and
                    all(isinstance(i, tuple) and len(i) == 2 for i in params)
                and
                    all(isinstance(k, str) and isinstance(v, (int, str)) for k, v in params)
            ) :
                paramsstr = "&".join("%s=%s" % (k, quote_url(str(v))) for k, v in params)
            else :
                raise TypeError("params are not a dict or tuple of suitable (key, value) pairs")
            #end if
        else :
            paramsstr = ""
        #end if
        url = self.url_base + self.prefix + path + ("", "?")[paramsstr != ""] + paramsstr
        if self.debug :
            sys.stderr.write("ARI request URL = %s\n" % url)
        #end if
        if data != None :
            if isinstance(data, (list, tuple, dict)) :
                data = json.dumps(data).encode()
            elif isinstance(data, (bytes, bytearray)) :
                pass
            else :
                raise TypeError("unsupported type %s for data" % type(data).__name__)
            #end if
            if self.debug :
                sys.stderr.write("ARI data = %s\n" % repr(data))
            #end if
            if method != RESTMETHOD.POST :
                raise ValueError("request body only allowed for POST")
            #end if
        #end if
        fail = None
        try :
            request = urllib.request.Request \
              (
                url,
                data = data,
                method = method.methodstr,
                headers =
                    {
                        "Authorization" : self.passwd.make_basic_auth(),
                        "Content-type" : "application/json",
                    }
              )
            if ASYNC :
                with await call_async(self.opener.open, (request,)) as req :
                    resp = await call_async(req.read, ())
                #end with
            else :
                with self.opener.open(request) as req :
                    resp = req.read()
                #end with
            #end if
        except urllib.error.HTTPError as reqfail :
            # replace with my own exception object just so I don’t get those
            # long tracebacks from the depths of urllib.
            fail = ARIError(reqfail.code, reqfail.reason, reqfail.headers)
        #end try
        if fail != None :
            raise fail
        #end if
        if self.debug :
            sys.stderr.write("raw resp = %s\n" % repr(resp))
        #end if
        if resp != b"" :
            result = json.loads(resp)
        else :
            result = None
        #end if
        return \
            result
    #end request

    class EventListener :
        "wrapper for WebSocket client connection that returns decoded events." \
        " You can use the fileno() method with select/poll to monitor for" \
        " incoming data, then call process() to read and process the data and" \
        " yield any decoded events."

        async def __new__(celf, parent, apps, subscribe_all, debug = False) :
            self = super().__new__(celf)
            self.debug = debug
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.ws = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)
            if ASYNC :
                await call_async(self.sock.connect, ((parent.host, parent.port),))
                self.sock.setblocking(False)
            else :
                self.sock.connect((parent.host, parent.port))
            #end if
            self.fileno = self.sock.fileno
            self.EOF = self.closing = False
            self.partial = ""
            req = self.ws.send \
              (
                wsevents.Request
                  (
                    host = parent.host,
                    target =
                            "%s/events?app=%s&subscribeAll=%d"
                        %
                            (parent.prefix, ",".join(apps), int(subscribe_all)),
                    extra_headers =
                        [
                            (
                                "Authorization",
                                parent.passwd.make_basic_auth(),
                            ),
                        ]
                  )
              )
            if ASYNC :
                await asyncio.get_running_loop().sock_sendall(self.sock, req)
            else :
                self.sock.sendall(req)
            #end if
            return \
                self
        #end __new__

        async def process(self) :
            "Call this when your event loop gets a notification that input is" \
            " pending on the WebSocket connection. It will yield any received" \
            " events."
            try :
                data = self.sock.recv(IOBUFSIZE, socket.MSG_DONTWAIT)
            except BlockingIOError :
                data = None
            #end try
            if data != None :
                if len(data) != 0 :
                    self.ws.receive_data(data)
                else :
                    self.EOF = True
                #end if
            #end if
            if ASYNC :
                loop = asyncio.get_running_loop()
            #end if
            events = iter(self.ws.events())
            while True :
                event = next(events, None)
                if event == None :
                    break
                if isinstance(event, wsevents.AcceptConnection) :
                    if self.debug :
                        sys.stderr.write("connection accepted\n")
                    #end if
                elif isinstance(event, wsevents.RejectConnection) :
                    raise RuntimeError("WebSockets connection rejected: code %d" % event.status_code)
                elif isinstance(event, wsevents.CloseConnection) :
                    if self.debug :
                        sys.stderr.write("connection closing\n")
                    #end if
                    if not self.closing :
                        self.closing = True
                        if ASYNC :
                            await loop.sock_sendall(self.sock, self.ws.send(event.response()))
                        else :
                            self.sock.sendall(self.sock, self.ws.send(event.response()))
                        #end if
                    #end if
                elif isinstance(event, wsevents.Ping) :
                    if ASYNC :
                        await loop.sock_sendall(self.ws.send(event.response()))
                    else :
                        self.sock.sendall(self.ws.send(event.response()))
                    #end if
                elif isinstance(event, wsevents.TextMessage) :
                    self.partial += event.data
                    if not event.message_finished :
                        break
                    if self.partial != "" :
                        result = json.loads(self.partial)
                        self.partial = ""
                    else :
                        result = None
                    #end if
                    yield result
                else :
                    raise RuntimeError("unexpected WebSocket event %s -- %s" % (type(event).__name__, repr(event)))
                #end if
            #end while
        #end process

        async def close(self) :
            if self.sock != None :
                if not self.closing :
                    self.closing = True
                    if ASYNC :
                        await asyncio.get_running_loop().sock_sendall(self.sock, self.ws.send(wsevents.CloseConnection(1000, "bye-bye")))
                    else :
                        self.sock.sendall(self.ws.send(wsevents.CloseConnection(1000, "bye-bye")))
                    #end if
                #end if
                if ASYNC :
                    async for event in self.process() :
                        pass
                    #end for
                else :
                    for event in self.process() :
                        pass
                    #end for
                #end if
                self.sock.close()
                self.sock = None
            #end if
        #end close

    #end EventListener

    async def listen(self, apps, subscribe_all = False) :
        "opens and returns a WebSocket connection to listen for ARI events." \
        " apps is a list/tuple of application names, and subscribe_all can" \
        " be set to True to enable these applications to receive all events."
        if ASYNC :
            result = await type(self).EventListener(self, apps, subscribe_all, debug = self.debug)
        else :
            result = type(self).EventListener(self, apps, subscribe_all, debug = self.debug)
        #end if
        return \
            result
    #end listen

#end Stasis

def_sync_async_classes(Stasis, "Stasis", "StasisAsync")

#+
# Asterisk console interface
#
# Note that the socket for this is normally only accessible to
# the root user.
#
# This is not actually documented as a public API. What I know
# about it I figured out from looking at the Asterisk source code,
# particularly main/asterisk.c. That file includes code for handling
# both the client and server ends of the connection. In that file, the
# client socket end is held in the variable “ast_consock”, and the
# server ends are managed in the “consoles” array, with elements of
# type “struct console”.
#-

CONSOLE_SOCKET_PATH = "/var/run/asterisk/asterisk.ctl"
  # default location, anyway

class Console :
    "opens a connection to the Asterisk console on the local machine."

    async def __new__(celf, socket_path = None, verbosity = 100) :
        self = super().__new__(celf)
        if socket_path == None :
            socket_path = CONSOLE_SOCKET_PATH
              # Why default dynamically, rather than statically?
              # So that caller can change value of global before
              # instantiating this class, if they wish.
        #end if
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if ASYNC :
            await call_async(conn.connect, (socket_path,))
        else :
            conn.connect(socket_path)
        #end if
        self.conn = conn
        self.to_send = b""
        self.received = b""
        self.EOF = False
        if verbosity != None :
            # without these commands, you will not see any
            # console messages returned.
            for cmd in \
                (
                    "core set verbose atleast %d silent" % verbosity,
                    "logger mute silent",
                ) \
            :
                self.send(cmd)
            #end for
        #end if
        return \
            self
    #end __new__

    def send(self, line) :
        "queues a command for transmission to the Asterisk console."
        b_line = line.encode()
        if 0 in b_line :
            raise ValueError("line contains embedded null")
        #end if
        self.to_send += b_line + b"\x00"
        return \
            self
    #end send

    def process(self, recv = True, send = True) :
        "receives any pending data from Asterisk (if recv) and also" \
        " sends it any pending commands from us (if send). Invoke this" \
        " from your event loop when monitoring of the conn socket indicates" \
        " that something is ready to be received or sent."
        received_something = sent_something = False
        if recv :
            while not self.EOF :
                try :
                    data = self.conn.recv(IOBUFSIZE, socket.MSG_DONTWAIT)
                except BlockingIOError :
                    data = None
                #end try
                if data == None :
                    break
                received_something = True # even if it’s only the EOF indication
                if len(data) != 0 :
                    self.received += data
                else :
                    self.EOF = True
                #end if
            #end while
        #end if
        if send :
            while True :
                if len(self.to_send) == 0 :
                    break
                try :
                    sent = self.conn.send(self.to_send, socket.MSG_DONTWAIT)
                except BlockingIOError :
                    sent = 0
                #end try
                if sent == 0 :
                    break
                sent_something = True
                self.to_send = self.to_send[sent:]
            #end while
        #end if
        return \
            (received_something, sent_something)
    #end process

    def get_response(self) :
        "returns any (partial) response line received so far, together with its" \
        " verbosity level. A complete line will end with a newline character," \
        " while a partial one will end with a null (not included). If neither" \
        " delimiter has been seen yet, then the empty string is returned."
        to_decode = self.received
        prefix = b""
        verbosity = 0
        if len(to_decode) != 0 and to_decode[0] >= 128 :
            prefix = to_decode[:1]
            verbosity = 256 - prefix[0] - 1
            to_decode = to_decode[1:]
        #end if
        line_end = to_decode.find(0)
        skip = 0
        if line_end >= 0 :
            # drop null from self.received without including it in
            # decoded result
            skip = 1
        else :
            line_end = to_decode.find(10)
            if line_end >= 0 :
                line_end += 1 # include newline in result
            #end if
        #end if
        if line_end >= 0 :
            to_decode = to_decode[:line_end]
            result = to_decode.decode()
            self.received = self.received[len(prefix) + len(to_decode) + skip:]
              # drop decoded data
        else :
            result = ""
        #end if
        return \
            (verbosity, result)
    #end get_response

    async def flush(self, timeout = None) :
        "ensures that any pending commands have been sent, or the given timeout" \
        " (if any) has elapsed. There may be returned responses available to be" \
        " retrieved after this."
        while True :
            if ASYNC :
                recv, send = await sock_wait_async \
                  (
                    sock = self.conn,
                    recv = True,
                    send = len(self.to_send) != 0,
                    timeout = timeout
                  )
            else :
                recv, send = sock_wait \
                  (
                    sock = self.conn,
                    recv = True,
                    send = len(self.to_send) != 0,
                    timeout = timeout
                  )
            #end if
            if not (recv or send) :
                # timeout
                break
            done = self.process(recv = recv, send = send)
            if len(self.to_send) == 0 :
                break
        #end while
        return \
            self
    #end flush

    async def close(self) :
        if self.conn != None :
            if ASYNC :
                await call_async(self.conn.close, ())
            else :
                self.conn.close()
            #end if
            self.conn = None
        #end if
    #end close

#end Console

def_sync_async_classes(Console, "Console", "ConsoleAsync")

#+
# Tidy up
#-

del ConditionalExpander, def_sync_async_classes
  # your work is done
del inspect, ast # not needed any more either
