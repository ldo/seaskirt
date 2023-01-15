#+
# Various useful Asterisk-related definitions.
#
# Copyright © 2007-2023 by Lawrence D'Oliveiro <ldo@geek-central.gen.nz>.
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
import time
import enum
import errno
from weakref import \
    ref as weak_ref
import socket
import ssl
import threading
import queue
import base64
import urllib.parse
import http.client
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
                returns = node.returns
              )
            if hasattr(node, "type_comment") :
                result.type_comment = node.type_comment
            #end if
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
                awaiting.set_exception(TimeoutError("async operation taking too long"))
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

class RequestQueue :
    "for making blocking requests nonblocking by passing them off to a worker" \
    " thread."

    def __init__(self) :
        self.loop = asyncio.get_running_loop()
        self.queue = queue.Queue()
        self.runner = None # only started on demand
        self.stopping = False
    #end __init__

    class Request :
        "represents an I/O request to be executed by the request-runner" \
        " thread, and includes a future so completion (or failure) can" \
        " be reported back to the initiating thread."

        def __init__(self, parent, fn, fnargs) :
            # fills out the Request and puts it on the thread queue.
            if parent.stopping :
                raise asyncio.InvalidStateError("queue is stopping--no new requests")
            #end if
            self.fn = fn
            self.fnargs = fnargs
            self.notify_done = parent.loop.create_future()
            parent.queue.put(self)
            if parent.runner == None :
                parent.runner = threading.Thread \
                  (
                    target = parent._do_run,
                    daemon = True
                  )
                parent.runner.start()
            #end if
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

    def request(self, fn, fnargs) :
        return \
            type(self).Request(self, fn, fnargs)
    #end request

    def _do_run(self) :
        # processes actual I/O requests on a separate thread.
        # A “None” queue element is treated as a request to terminate.
        while True :
            elt = self.queue.get()
            if elt == None :
                self.queue.task_done()
                break
            #end if
            try :
                res = elt.fn(*elt.fnargs)
            except Exception as err :
                fail = err
                res = None
            else :
                fail = None
            #end try
            self.loop.call_soon_threadsafe(elt.request_done, res, fail)
            self.queue.task_done()
        #end while
        self.runner = None
    #end _do_run

    def terminate(self) :
        "tells the processor thread to terminate."
        if not self.stopping :
            self.stopping = True
            self.queue.put(None)
        #end if
    #end terminate

#end RequestQueue

class AsyncProcessor :
    "wrapper around Python I/O-related objects which makes all (relevant) calls" \
    " asynchronous by passing them off to a dedicated request-runner thread."

    def __init__(self, io_obj, funcdefs) :

        def def_queue_request(name, nrargs) :
            # defines an async wrapper around the given method of io_obj
            # as a method of this class with the same name.

            io_meth = getattr(io_obj, name)

            async def reqfunc(*args) :
                if len(args) != nrargs :
                    raise TypeError \
                      (
                            "%s.%s expects %d args, not %d"
                        %
                            (type(io_obj).__name__, name, nrargs, len(args))
                      )
                #end if
                return \
                    await self.requests.request(io_meth, args)
            #end reqfunc

        #begin def_queue_request
            reqfunc.__name__ = name
            setattr(self, name, reqfunc)
        #end def_queue_request

    #begin __init__
        if (
                not isinstance(funcdefs, (list, tuple))
            or
                not all
                  (
                        isinstance(name, str)
                    and
                        not name.startswith("_")
                    and
                        name not in
                            {
                                "fileno", "io_obj", "requests", "terminate",
                            }
                          # attributes of base class
                    and
                        isinstance(nrargs, int)
                    and
                        nrargs >= 0
                    for name, nrargs in funcdefs
                  )
        ) :
            raise TypeError("funcdefs must be sequence of («name», «nrargs») pairs")
        #end if
        for name, nrargs in funcdefs :
            def_queue_request(name, nrargs)
        #end for
        self.io_obj = io_obj
        self.requests = RequestQueue()
    #end __init__

    def fileno(self) :
        return \
            self.io_obj.fileno()
    #end fileno

    def terminate(self) :
        "tells the processor thread to terminate."
        self.requests.terminate()
    #end terminate

#end AsyncProcessor

class AsyncFile(AsyncProcessor) :
    "AsyncProcessor subclass for wrapping Python file objects."

    def __init__(self, fv) :
        super().__init__ \
          (
            fv,
            [ # list all the calls I actually use
                ("read", 1),
                ("readline", 0),
                ("write", 1),
                ("flush", 0),
                ("close", 0),
            ]
          )
    #end __init__

#end AsyncFile

class SOCK_NEED(enum.Enum) :
    "need to wait for socket to allow I/O of this type before further" \
    " communication can proceed."
    NOTHING = 0 # I/O can proceed
    READABLE = 1 # wait for socket to become readable
    WRITABLE = 2 # wait for socket to become writable
#end SOCK_NEED

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

class AbsoluteTimeout :
    "given a relative timeout in seconds from the time of" \
    " instantiation, produces any number of successive relative" \
    " timeouts that always end at the same absolute time. This way," \
    " the timeout applies to a complete sequence of operations being" \
    " performed. If the given timeout is None, then returned relative" \
    " timeouts are also None."

    def __init__(self, timeout) :
        if timeout != None :
            self.deadline = time.monotonic() + timeout
        else :
            self.deadline = None
        #end if
    #end __init__

    @property
    def timeout(self) :
        if self.deadline != None :
            result = max(self.deadline - time.monotonic(), 0)
        else :
            result = None
        #end if
        return \
            result
    #end timeout

#end AbsoluteTimeout

#+
# Socket wrapper classes. These try to offer as uniform
# an abstraction as possible that covers both encrypted
# and non-encrypted sockets.
#-

# common method definitions which could go in a subclass of
# SocketWrapper/SocketWrapperAsync/SSLSocketWrapper/SSLSocketWrapperAsync,
# if such a thing could exist:

def _poll_register(self, poll) :
    "registers this socket with the given poll object, if it needs to. Returns" \
    " True iff registration was done."
    register = self.sock_need != SOCK_NEED.NOTHING
    if register :
        poll.register \
          (
            self.sock,
            {
                SOCK_NEED.READABLE : select.POLLIN,
                SOCK_NEED.WRITABLE : select.POLLOUT,
            }[self.sock_need]
          )
    #end if
    return \
        register
#end _poll_register
_poll_register.__name__ = "poll_register"

def _io_wait_sync(self, timeout = None) :
    "blocks as appropriate until the socket is ready to try immediate-mode" \
    " reading or writing, as determined by the state saved from the last" \
    " recvimmed/sendimmed call."
    if self.sock_need != SOCK_NEED.NOTHING :
        poll = select.poll()
        assert self.poll_register(poll)
        ready = poll.poll(timeout)
        if len(ready) > 0 :
            assert len(ready) == 1
            ready, = ready
            assert ready[0] == self.sock.fileno()
            flags = ready[1]
            recv = flags & select.POLLIN != 0
            send = flags & select.POLLOUT != 0
        else :
            recv = send = False
        #end if
        self.sock_need = SOCK_NEED.NOTHING
    else :
        recv = send = True
    #end if
    return \
        recv, send
#end _io_wait_sync
_io_wait_sync.__name__ = "io_wait"

async def _io_wait_async(self, timeout = None) :
    "blocks as appropriate until the socket is ready to try immediate-mode" \
    " reading or writing, as determined by the state saved from the last" \
    " recvimmed/sendimmed call."
    if self.sock_need != SOCK_NEED.NOTHING :
        recv, send = await sock_wait_async \
          (
            self.sock.fileno(),
            recv = self.sock_need == SOCK_NEED.READABLE,
            send = self.sock_need == SOCK_NEED.WRITABLE,
            timeout = timeout
          )
        self.sock_need = SOCK_NEED.NOTHING
    else :
        recv = send = True
    #end if
    return \
        recv, send
#end _io_wait_async
_io_wait_async.__name__ = "io_wait"

class SocketWrapper :
    "a wrapper around unencrypted socket connections, providing sync or async" \
    " transfers with optional timeouts. Note no fileno() method is provided, for" \
    " compatibility with SSLSocketWrapper."

    def __init__(self) :
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock = sock
        self.sock_need = SOCK_NEED.NOTHING
          # set by immediate-mode calls, used by I/O-polling hooks to decide what to wait for
    #end __init__

    async def connect(self, addr, timeout = None) :
        if ASYNC :
            await call_async(self.sock.connect, (addr,), timeout = timeout)
        else :
            # TODO: timeout
            self.sock.connect(addr)
        #end if
    #end connect

    async def recv(self, nrbytes, timeout = None) :
        self.sock_need = SOCK_NEED.NOTHING
        deadline = AbsoluteTimeout(timeout)
        while True :
            try :
                got = self.sock.recv(nrbytes, socket.MSG_DONTWAIT)
            except BlockingIOError :
                got = None
            #end try
            if got != None :
                timed_out = False
                break
            #end if
            if deadline.timeout == 0 :
                timed_out = True
                break
            #end if
            if ASYNC :
                recv = (await sock_wait_async(self.sock, True, False, deadline.timeout))[0]
            else :
                recv = sock_wait(self.sock, True, False, deadline.timeout)[0]
            #end if
            if not recv :
                timed_out = True
                break
            #end if
        #end while
        if timed_out :
            nr_bytes = None
        #end if
        return \
            got
    #end recv

    def recvimmed(self, nrbytes) :
        self.sock_need = SOCK_NEED.NOTHING
        try :
            data = self.sock.recv(nrbytes, socket.MSG_DONTWAIT)
        except BlockingIOError :
            data = None
            self.sock_need = SOCK_NEED.READABLE
        #end try
        return \
            data
    #end recvimmed

    async def sendall(self, data, timeout = None) :
        self.sock_need = SOCK_NEED.NOTHING
        deadline = AbsoluteTimeout(timeout)
        while True :
            try :
                sent = self.sock.send(data, socket.MSG_DONTWAIT)
            except BlockingIOError :
                sent = 0
            #end try
            data = data[sent:]
            if len(data) == 0 :
                timed_out = False
                break
            #end if
            if ASYNC :
                send = (await sock_wait_async(self.sock, False, True, deadline.timeout))[1]
            else :
                send = sock_wait(self.sock, False, True, deadline.timeout)[1]
            #end if
            if not send :
                timed_out = True
                break
            #end if
        #end while
        if timed_out :
            raise TimeoutError("socket taking too long to send")
        #end if
    #end sendall

    def sendimmed(self, data) :
        self.sock_need = SOCK_NEED.NOTHING
        try :
            sent = self.sock.send(data, socket.MSG_DONTWAIT)
        except BlockingIOError :
            sent = 0
            self.sock_need = SOCK_NEED.WRITABLE
        #end try
        return \
            sent
    #end sendimmed

    async def close(self) :
        if self.sock != None :
            if ASYNC :
                await call_async(self.sock.close, ())
            else :
                self.sock.close()
            #end if
            self.sock = None
        #end if
    #end close

    poll_register = _poll_register

#end SocketWrapper

def_sync_async_classes(SocketWrapper, "SocketWrapper", "SocketWrapperAsync")
SocketWrapper.io_wait = _io_wait_sync
SocketWrapperAsync.io_wait = _io_wait_async

def get_ssl_context(ssl_context) :
    if isinstance(ssl_context, (bytes, bytearray, str)) :
        ca_path = ssl_context
        if ca_path.endswith("/") :
            ca_file = None
        else :
            ca_file = ca_path
            ca_path = None
        #end if
        ssl_context = ssl.SSLContext(protocol = ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.load_verify_locations(ca_file, ca_path)
    elif not isinstance(ssl_context, ssl.SSLContext) :
        raise TypeError \
          (
            "ssl_context must be SSLContext or file/directory pathname for CA cert(s)"
          )
    #end if
    return \
        ssl_context
#end get_ssl_context

class SSLSocketWrapper :
    "a wrapper around TLS-encrypted socket connections, providing sync or async" \
    " transfers with optional timeouts. ssl_context can be specified as the path" \
    " to the CA cert file or directory, or a preconfigured ssl.SSLContext object." \
    " Note no fileno() method is provided, since SSL may require writes before you" \
    " can read more, or vice versa; so use poll_register() and io_wait() calls" \
    " if you want to hook into an event loop safely."

    def __init__(self, ssl_context) :
        self.ssl_context = get_ssl_context(ssl_context)
        self.sock = self.ssl_context.wrap_socket \
          (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            server_side = False,
            do_handshake_on_connect = False
              # defer handshake until nonblocking mode has been enabled
          )
        self.sock_need = SOCK_NEED.NOTHING
          # set by immediate-mode calls, used by I/O-polling hooks to decide what to wait for
        if ASYNC :
            self.wait_avail =  None
        #end if
    #end __init__

    class DoAgain(Exception) :
        # an indication that the I/O operation has only been partially done so far,
        # and needs to be repeated.
        pass
    #end DoAgain

    async def _do_io(self, opname, meth, args, timeout, signal_timeout = True) :
        self.sock_need = SOCK_NEED.NOTHING
        DoAgain = type(self).DoAgain
        deadline = AbsoluteTimeout(timeout)
        if ASYNC :
            my_wait_avail = asyncio.get_running_loop().create_future()
            if self.wait_avail != None :
                # somebody else using this socket -- wait till they’re done
                await self.wait_avail
            #end if
            assert self.wait_avail == None
            self.wait_avail = my_wait_avail
              # claim ownership of socket
        #end if
        try :
            while True :
                sock_need = None
                do_again = False
                try :
                    try :
                        result = meth(*args)
                    except DoAgain :
                        do_again = True
                    #end try
                except ssl.SSLWantReadError :
                    sock_need = SOCK_NEED.READABLE
                except ssl.SSLWantWriteError :
                    sock_need = SOCK_NEED.WRITABLE
                #end try
                if sock_need == None and not do_again :
                    timed_out = False
                    break
                #end if
                if sock_need != None :
                    if ASYNC :
                        recv, send = await sock_wait_async \
                          (
                            self.sock.fileno(),
                            recv = sock_need == SOCK_NEED.READABLE,
                            send = sock_need == SOCK_NEED.WRITABLE,
                            timeout = deadline.timeout
                          )
                    else :
                        recv, send = sock_wait \
                          (
                            self.sock.fileno(),
                            recv = sock_need == SOCK_NEED.READABLE,
                            send = sock_need == SOCK_NEED.WRITABLE,
                            timeout = deadline.timeout
                          )
                    #end if
                    if not (recv or send) :
                        timed_out = True
                        break
                    #end if
                #end if
            #end while
        finally :
            if ASYNC :
                my_wait_avail.set_result(None)
                assert self.wait_avail == my_wait_avail
                self.wait_avail = None
            #end if
        #end try
        if timed_out :
            if signal_timeout :
                raise TimeoutError("socket taking too long to %s" % opname)
            else :
                result = None
            #end if
        #end if
        return \
            result
    #end _do_io

    async def connect(self, addr, timeout = None) :
        if ASYNC :
            await call_async(self.sock.connect, (addr,), timeout = timeout)
            self.sock.setblocking(False)
            await self._do_io("connect handshake", self.sock.do_handshake, (), timeout)
        else :
            # TODO: timeout
            self.sock.connect(addr)
            self.sock.setblocking(False)
            self._do_io("connect handshake", self.sock.do_handshake, (), timeout)
        #end if
    #end connect

    def recv(self, nrbytes, timeout = None) :
        return \
            self._do_io("receive", self.sock.recv, (nrbytes,), timeout, signal_timeout = False)
    #end recv

    def recvimmed(self, nrbytes) :
        self.sock_need = SOCK_NEED.NOTHING
        try :
            data = self.sock.recv(nrbytes)
        except ssl.SSLWantReadError:
            data = None
            self.sock_need = SOCK_NEED.READABLE
        except ssl.SSLWantWriteError :
            data = None
            self.sock_need = SOCK_NEED.WRITABLE
        #end try
        return \
            data
    #end recvimmed

    def sendall(self, data, timeout = None) :

        DoAgain = type(self).DoAgain
        sending = None # context for resuming partial sends

        def send_some() :
            sent = self.sock.send(sending["data"])
            sending["data"] = sending["data"][sent:]
            if len(sending["data"]) != 0 :
                raise DoAgain
            #end if
        #end send_some

    #begin sendall
        sending = \
            {
                "data" : data,
            }
        return \
            self._do_io("send", send_some, (), timeout)
    #end sendall

    def sendimmed(self, data) :
        self.sock_need = SOCK_NEED.NOTHING
        try :
            sent = self.sock.send(data)
        except ssl.SSLWantReadError:
            sent = 0
            self.sock_need = SOCK_NEED.READABLE
        except ssl.SSLWantWriteError :
            sent = 0
            self.sock_need = SOCK_NEED.WRITABLE
        #end try
        return \
            sent
    #end sendimmed

    def shutdown(self, how) :
        return \
            self._do_io("shutdown", self.sock.shutdown, (how,), None)
    #end shutdown

    def close(self) :
        return \
            self._do_io("close", self.sock.close, (), None)
    #end close

    poll_register = _poll_register

#end SSLSocketWrapper

def_sync_async_classes(SSLSocketWrapper, "SSLSocketWrapper", "SSLSocketWrapperAsync")
SSLSocketWrapper.io_wait = _io_wait_sync
SSLSocketWrapperAsync.io_wait = _io_wait_async

def make_socket_wrapper(ssl_context, is_async) :
    "interprets the various ways of specifying TLS/SSL info, as the path" \
    " to a CA file or a directory containing CA files, or directly as a" \
    " preconfigured ssl.Context object, returning an ssl.Context object in" \
    " all cases."
    if ssl_context != None :
        result = (SSLSocketWrapper, SSLSocketWrapperAsync)[is_async](ssl_context)
    else :
        result = (SocketWrapper, SocketWrapperAsync)[is_async]()
    #end if
    return \
        result
#end make_socket_wrapper

del _poll_register, _io_wait_sync, _io_wait_async # only needed above

#+
# Asterisk Manager Interface
#-

AMI_DEFAULT_PLAINTEXT_PORT = 5038
AMI_DEFAULT_TLS_PORT = 5039

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

    async def __new__(celf, host = "127.0.0.1", port = None, *, ssl_context = None, username, password, want_events = False, id_gen = None, timeout = None, debug = False) :
        "opens connection, receives initial Hello message from Asterisk, and does" \
        " initial mandatory authentication handshake."
        self = super().__new__(celf)
        self.debug = debug
        self.timeout = timeout
        if id_gen != None and not hasattr(id_gen, "__next__") :
            raise TypeError("id_gen is not an iterator")
        #end if
        self.id_gen = id_gen
        self.last_request_id = None
        if port == None :
            port = (AMI_DEFAULT_PLAINTEXT_PORT, AMI_DEFAULT_TLS_PORT)[ssl_context != None]
        #end if
        if ASYNC :
            self.sock = make_socket_wrapper(ssl_context, True)
            await self.sock.connect((host, port), timeout)
        else :
            self.sock = make_socket_wrapper(ssl_context, False)
            self.sock.connect((host, port), timeout)
        #end if
        self.buff = ""
        self.EOF = False
        while True : # get initial hello msg
            if ASYNC :
                more = await self.sock.recv(SMALL_IOBUFSIZE)
            else :
                more = self.sock.recv(SMALL_IOBUFSIZE)
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
        if self.sock != None :
            sock = self.sock
            self.sock = None # stop further async calls
            if ASYNC :
                # await sock.shutdown(socket.SHUT_RDWR)
                  # not necessary?
                await sock.close()
            else :
                # sock.shutdown(socket.SHUT_RDWR)
                  # not necessary?
                sock.close()
            #end if
        #end if
    #end close

    def poll_register(self, poll) :
        return \
            self.sock.poll_register(poll)
    #end poll_register

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
            await self.sock.sendall(to_send.encode(), self.timeout)
        else :
            self.sock.sendall(to_send.encode(), self.timeout)
        #end if
    #end send_request

    async def get_response(self, timeout = None) :
        "reads and parses another response from the Asterisk Manager connection." \
        " This can be a reply to a prior request, or it can be an unsolicited event" \
        " notification, if you have enabled those on this connection."
        NL = self.NL
        if timeout == None :
            timeout = self.timeout
        #end if
        response = None # to begin with
        while True :
            endpos = self.buff.find(NL + NL)
            if endpos >= 0 :
                # got at least one complete response
                resp = self.buff[:endpos + len(NL)] # include one NL at end
                self.buff = self.buff[endpos + 2 * len(NL):]
                response = {}
                while True :
                    split, resp = resp.split(NL, 1)
                    keyword, value = split.split(": ", 1)
                    if keyword in response :
                        response[keyword] += "\n" + value
                    else :
                        response[keyword] = value
                    #end if
                    if resp == "" :
                        break
                #end while
                break
            #end if
            # need more input
            if self.EOF :
                raise EOFError("Asterisk Manager connection EOF")
            #end if
            if ASYNC :
                more = await self.sock.recv(IOBUFSIZE, timeout)
            else :
                more = self.sock.recv(IOBUFSIZE, timeout)
            #end if
            if more == None :
                # timed out
                break
            self.buff += more.decode()
            if self.debug :
                sys.stderr.write \
                  (
                    "Manager got (%u): \"%s\"\n" % (len(self.buff), self.buff)
                  )
            #end if
            if len(more) == 0 :
                self.EOF = True
            #end if
        #end while
        return \
            response
    #end get_response

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
            if next_response == None :
                raise TimeoutError("Manager transaction reply taking too long")
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
                    more = await self.sock.recv(IOBUFSIZE, self.timeout)
                else :
                    more = self.sock.recv(IOBUFSIZE, self.timeout)
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

    async def __new__(celf, *, from_asterisk = None, to_asterisk = None, args = None, with_audio_in = False, debug = False) :
        "from_asterisk and to_asterisk are file objects to use to communicate" \
        " with Asterisk; default to sys.stdin and sys.stdout if not specified, while" \
        " args are taken from sys.argv if not specified.\n" \
        "with_audio_in indicates whether to set audio_in attribute to a file object for" \
        " reading linear PCM audio from the channel (only possible if the script" \
        " was invoked via the EAGI application command).\n" \
        "agi_vars attribute will be set to a dictionary containing all the initial" \
        " AGI variable definitions passed from Asterisk."
        self = super().__new__(celf)
        self.debug = debug
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
            self.sock = socket.socket()
            self.sock.bind((bindaddr, port))
            self.sock.listen(maxlisten)
        #end __init__

        def fileno(self) :
            return \
                self.sock.fileno()
        #end fileno

        async def accept(self) :
            if ASYNC :
                sock, peer = await call_async(self.sock.accept, ())
            else :
                sock, peer = self.sock.accept()
            #end if
            connin = os.fdopen(os.dup(sock.fileno()), "rt")
            connout = os.fdopen(os.dup(sock.fileno()), "wt")
            if self.debug :
                sys.stderr.write("%s.Listener connection from %s\n" % (self.parent.__name__, peer))
            #end if
            if ASYNC :
                result = await self.parent(from_asterisk = connin, to_asterisk = connout, debug = self.debug)
            else :
                result = self.parent(from_asterisk = connin, to_asterisk = connout, debug = self.debug)
            #end if
            return \
                result
        #end listen

        async def close(self) :
            if self.sock != None :
                if ASYNC :
                    await call_async(self.sock.close, ())
                else :
                    self.sock.close()
                #end if
                self.sock = None
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
                self.from_asterisk.terminate()
            else :
                self.from_asterisk.close()
            #end if
            self.from_asterisk = None
        #end if
        if self.to_asterisk != None :
            if ASYNC :
                await self.to_asterisk.close()
                self.to_asterisk.terminate()
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

HTTP_DEFAULT_PLAINTEXT_PORT = 8088
HTTP_DEFAULT_TLS_PORT = 8089

class ARIError(Exception) :
    "just to identify HTTP error codes returned from Asterisk ARI."

    def __init__(self, errno, msg) :
        self.errno = errno # integer or None
        self.msg = msg
    #end __init__

    def __str__(self) :
        if self.errno != None :
            result = "ARI Error %d: %s" % (self.errno, self.msg)
        else :
            result = "ARI Error: %s" % self.msg
        #end if
        return \
            result
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

class ARIPasswordHandler :
    "only holds a single username/password pair for the expected Asterisk realm."

    def __init__(self, username, password) :
        self.realm = "Asterisk REST Interface"
        self.username = username
        self.password = password
    #end __init__

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

    async def __new__(celf, host = "127.0.0.1", port = None, *, prefix = "/ari", username, password, ssl_context = None, timeout = None, debug = False) :
        # doesn’t actually need to be async, defined async just to
        # be consistent with other main API classes.
        self = super().__new__(celf)
        if ssl_context != None :
            self.ssl_context = get_ssl_context(ssl_context)
        else :
            self.ssl_context = None
        #end if
        if port == None :
            port = (HTTP_DEFAULT_PLAINTEXT_PORT, HTTP_DEFAULT_TLS_PORT)[self.ssl_context != None]
        #end if
        if prefix != "" and not prefix.startswith("/") :
            raise ValueError("nonempty prefix must start with “/”")
        #end if
        self.host = host
        self.port = port
        self.prefix = prefix
        self.debug = debug
        self.url_base = \
            "%s://%s:%d" % (("http", "https")[self.ssl_context != None], self.host, self.port)
        # Use http.client instead of urllib.request so that I can ask for
        # persistent connections.
        if self.ssl_context != None :
            self.http = http.client.HTTPSConnection \
              (
                host = self.host,
                port = self.port,
                context = self.ssl_context,
                timeout = timeout
              )
        else :
            self.http = http.client.HTTPConnection \
              (
                host = self.host,
                port = self.port,
                timeout = timeout
              )
        #end if
        if self.debug :
            self.http.set_debuglevel(9)
        #end if
        self.passwd = ARIPasswordHandler(username, password)
        if ASYNC :
            self.requests = RequestQueue()
        else :
            self.requests = None
        #end if
        return \
            self
    #end __new__

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
        url = self.prefix + path + ("", "?")[paramsstr != ""] + paramsstr
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
            if data != b"" and method != RESTMETHOD.POST :
                raise ValueError("request body only allowed for POST")
            #end if
        else :
            data = b""
        #end if
        fail = None
        try :
            # Managing requests via an http.client.HTTP{,S}Connection object
            # is a bit fiddly. For some reason I couldn’t get the higher-level
            # request() method to actually communicate with the server, so I
            # use the lower-level putrequest()/putheader()/etc methods instead.
            # And there is no autoreconnect facility if the persistent connection
            # gets timed out by the other side. So I have to go through the
            # whole sequence of putting headers and body, only to catch it if it
            # fails with a BrokenPipeError on the final send() call, whereupon
            # I have to reconnect before trying again.
            headers = \
                {
                    "Authorization" : self.passwd.make_basic_auth(),
                    "Content-type" : "application/json",
                    "Connection" : "Keep-Alive",
                    "Content-length" : "%d" % len(data),
                }
            repeated = False
            def do_request(reconnect) :
                if reconnect :
                    self.http.close() # force a reconnect
                #end if
                self.http.putrequest(method.methodstr, url)
                for key in sorted(headers.keys()) :
                    self.http.putheader(key, headers[key])
                #end for
                self.http.endheaders()
                self.http.send(data)
                resp = self.http.getresponse()
                return \
                    resp
            #end do_request
            while True :
                try :
                    if ASYNC :
                        resp = await self.requests.request(do_request, (repeated,))
                    else :
                        resp = do_request(repeated)
                    #end if
                except (BrokenPipeError, http.client.RemoteDisconnected) :
                    if repeated :
                        raise # only retry once per request
                    # need to close, reconnect and start again
                    if self.debug :
                        sys.stderr.write("auto-reopening HTTP connection\n")
                    #end if
                    repeated = True
                else :
                    break
                #end try
            #end while
        # Replace exceptions with my own exception object just so I don’t
        # get those long tracebacks from the depths of http.client or
        # elsewhere.
        except http.client.HTTPException as reqfail:
            fail = ARIError(None, repr(reqfail))
        except ConnectionError as reqfail :
            fail = ARIError(reqfail.errno, reqfail.strerror)
        except TimeoutError :
            fail = ARIError(None, "server taking too long to respond")
        #end try
        if fail != None :
            raise fail
        #end if
        resptype = resp.getheader("content-type")
        respdata = resp.read()
        if len(respdata) != 0 and resptype != "application/json" :
            raise ARIError(None, "unexpected nonempty content type %s data %s" % (resptype, repr(respdata)))
        #end if
        if self.debug :
            sys.stderr.write("Stasis resp headers = %s\n" % repr(resp.getheaders()))
            sys.stderr.write("Stasis raw resp = %s\n" % repr(respdata))
        #end if
        if respdata != b"" :
            result = json.loads(respdata)
        else :
            result = None
        #end if
        return \
            result
    #end request

    async def close(self) :
        if self.http != None :
            self.requests.terminate()
            conn = self.http
            self.http = None # stop further async calls
            if ASYNC :
                await call_async(conn.close, ())
            else :
                conn.close()
            #end if
        #end if
    #end close

    class EventListener :
        "wrapper for WebSocket client connection that returns decoded events."

        async def __new__(celf, parent, apps, subscribe_all, *, timeout = None, debug = False) :
            self = super().__new__(celf)
            self.debug = debug
            if ASYNC :
                self.sock = make_socket_wrapper(parent.ssl_context, True)
                await self.sock.connect((parent.host, parent.port))
            else :
                self.sock = make_socket_wrapper(parent.ssl_context, False)
                self.sock.connect((parent.host, parent.port))
            #end if
            self.ws = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)
            self.using_ssl = parent.ssl_context != None
            self.EOF = self.closing = False
            self.partial = ""
            self.current_reading = None
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
                await self.sock.sendall(req, timeout)
            else :
                self.sock.sendall(req, timeout)
            #end if
            return \
                self
        #end __new__

        def poll_register(self, poll) :
            return \
                self.sock.poll_register(poll)
        #end poll_register

        async def wait_readable(self, timeout = None) :
            "low-level call: lets you block until further events are available" \
            " to be read, or the specified timeout elapses. Returns True iff" \
            " you should try reading more input."
            if self.EOF :
                raise EOFError("Asterisk WebSocket wait EOF")
            #end if
            if ASYNC :
                readable, writable = await self.sock.io_wait(timeout)
            else :
                readable, writable = self.sock.io_wait(timeout)
            #end if
            return \
                readable or self.using_ssl and writable
        #end wait_readable

        async def process(self) :
            "low-level call, for when your event loop gets a notification that" \
            " input is pending on the WebSocket connection. It will yield any" \
            " received events."
            if not self.EOF :
                data = self.sock.recvimmed(IOBUFSIZE)
                if data != None :
                    if len(data) != 0 :
                        self.ws.receive_data(data)
                    else :
                        self.EOF = True
                    #end if
                #end if
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
                    raise RuntimeError \
                      (
                        "WebSockets connection rejected: code %d" % event.status_code
                      )
                elif isinstance(event, wsevents.CloseConnection) :
                    if self.debug :
                        sys.stderr.write("connection closing\n")
                    #end if
                    if not self.closing :
                        self.closing = True
                        if ASYNC :
                            await self.sock.sendall(self.sock, self.ws.send(event.response()))
                        else :
                            self.sock.sendall(self.sock, self.ws.send(event.response()))
                        #end if
                    #end if
                elif isinstance(event, wsevents.Ping) :
                    if ASYNC :
                        await self.sock.sendall(self.ws.send(event.response()))
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
                    raise RuntimeError \
                      (
                            "unexpected WebSocket event %s -- %s"
                        %
                            (type(event).__name__, repr(event))
                      )
                #end if
            #end while
        #end process

        async def get_event(self, timeout = None) :
            "high-level call: retrieves the next event, automatically waiting" \
            " if necessary. Returns None on timeout if timeout was specified," \
            " else waits indefinitely."
            assert not self.closing
            deadline = AbsoluteTimeout(timeout)
            while True :
                if self.current_reading != None :
                    # continuing to process data previously received
                    if ASYNC :
                        # evt = await anext(self.current_reading, None)
                          # only available in Python 3.10 or later
                        try :
                            evt = await self.current_reading.__anext__()
                        except StopAsyncIteration :
                            evt = None
                        #end try
                    else :
                        evt = next(self.current_reading, None)
                    #end if
                    if evt != None :
                        break
                    self.current_reading = None # iterator exhausted
                #end if
                if self.EOF :
                    raise EOFError("Asterisk WebSocket connection EOF")
                #end if
                # try to get some more data
                if ASYNC :
                    readable = await self.wait_readable(deadline.timeout)
                else :
                    readable = self.wait_readable(deadline.timeout)
                #end if
                if not readable :
                    # timeout
                    evt = None
                    break
                #end if
                if deadline.deadline != None :
                    data = self.sock.recvimmed(IOBUFSIZE)
                    if data == None and (not self.using_ssl or time.monotonic() > deadline.deadline) :
                        # timeout, but give SSL a chance to keep reporting want-more exceptions.
                        evt = None
                        break
                    #end if
                else :
                    if ASYNC :
                        data = await self.sock.recv(IOBUFSIZE)
                    else :
                        data = self.sock.recv(IOBUFSIZE)
                    #end if
                #end if
                if data != None :
                    if len(data) != 0 :
                        self.ws.receive_data(data)
                    else :
                        self.EOF = True
                    #end if
                    self.current_reading = self.process()
                #end if
            #end while
            return \
                evt
        #end get_event

        async def close(self) :
            if self.sock != None :
                if self.current_reading != None :
                    # gobble pending WebSocket messages
                    if ASYNC :
                        while True :
                            try :
                                await self.current_reading.__anext__()
                            except StopAsyncIteration :
                                break
                            #end try
                        #end while
                    else :
                        while next(self.current_reading, None) != None :
                            pass
                        #end while
                    #end if
                #end if
                if not self.closing :
                    self.closing = True
                    msg = self.ws.send(wsevents.CloseConnection(1000, "bye-bye"))
                    if ASYNC :
                        await self.sock.sendall(msg)
                    else :
                        self.sock.sendall(msg)
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
                if ASYNC :
                    await self.sock.close()
                else :
                    self.sock.close()
                #end if
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
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if ASYNC :
            await call_async(sock.connect, (socket_path,))
        else :
            sock.connect(socket_path)
        #end if
        self.sock = sock
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
        "low-level call: receives any pending data from Asterisk (if recv)" \
        " and also sends it any pending commands from us (if send). You can" \
        " invoke this from your event loop when monitoring of the socket" \
        " indicates that something is ready to be received or sent."
        received_something = sent_something = False
        if recv :
            while not self.EOF :
                data = self.sock.recvimmed(IOBUFSIZE)
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
                sent = self.sock.sendimmed(self.to_send)
                if sent == 0 :
                    break
                sent_something = True
                self.to_send = self.to_send[sent:]
            #end while
        #end if
        return \
            (received_something, sent_something)
    #end process

    async def flush(self, timeout = None) :
        "low-level call: ensures that any pending commands have been sent," \
        " or the given timeout (if any) has elapsed. There may be returned" \
        " responses available to be retrieved after this."
        while True :
            if ASYNC :
                recv, send = await sock_wait_async \
                  (
                    sock = self.sock,
                    recv = True,
                    send = len(self.to_send) != 0,
                    timeout = timeout
                  )
            else :
                recv, send = sock_wait \
                  (
                    sock = self.sock,
                    recv = True,
                    send = len(self.to_send) != 0,
                    timeout = timeout
                  )
            #end if
            if not (recv or send) :
                # timeout
                break
            self.process(recv = recv, send = send)
            if len(self.to_send) == 0 :
                break
        #end while
        return \
            self
    #end flush

    async def get_response(self, timeout = None) :
        "high-level call: returns any (partial) response line received so far," \
        " together with its verbosity level. A complete line will end with a" \
        " newline character, while a partial one will end with a null (not" \
        " included). If neither delimiter has been seen before the timeout," \
        " then (None, None) is returned."
        recv = True # to begin with
        while True :
            if recv :
                to_decode = self.received
                prefix = b""
                verbosity = 0
                if len(to_decode) != 0 and to_decode[0] >= 128 :
                    prefix = to_decode[:1]
                    verbosity = 256 - prefix[0] - 1
                    to_decode = to_decode[1:]
                #end if
                if len(to_decode) > 0 :
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
                        break
                    #end if
                #end if
            #end if
            if ASYNC :
                recv, send = await sock_wait_async \
                  (
                    sock = self.sock,
                    recv = True,
                    send = len(self.to_send) != 0,
                    timeout = timeout
                  )
            else :
                recv, send = sock_wait \
                  (
                    sock = self.sock,
                    recv = True,
                    send = len(self.to_send) != 0,
                    timeout = timeout
                  )
            #end if
            if not (recv or send) :
                # nothing more happening before timeout
                verbosity = result = None
                break
            #end if
            recv, send = self.process(recv, send)
        #end while
        return \
            (verbosity, result)
    #end get_response

    async def close(self) :
        if self.sock != None :
            if ASYNC :
                await call_async(self.sock.close, ())
            else :
                self.sock.close()
            #end if
            self.sock = None
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
