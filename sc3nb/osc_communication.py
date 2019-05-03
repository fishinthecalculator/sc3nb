"""Classes and functions to communicate with SuperCollider via
OSC messages over UDP
"""

import errno
import logging
import select
import socket
import threading
import time
from queue import Empty, Queue

from random import randint
from pythonosc import (dispatcher, osc_bundle_builder, osc_message,
                       osc_message_builder, osc_server)

from .tools import parse_sclang_blob

SCSYNTH_DEFAULT_PORT = 57110
SCLANG_DEFAULT_PORT = 57120

OSCCOM_DEFAULT_PORT = 57130


def build_bundle(timetag, msg_addr, msg_args):
    """Builds pythonsosc OSC bundle

    Arguments:
            timetag {int} -- Time at which bundle content
                             should be executed, either
                             in absolute or relative time.
                             relative time is assumed
                             if the time tag is smaller
                             then 1e6
            msg_addr {str} -- SuperCollider address
                              E.g. '/s_new'
            msg_args {list} -- List of arguments to add
                               to message

    Returns:
        OscBundle -- Bundle ready to be sent
    """

    if msg_args is None:
            msg_args = []

    if timetag < 1e6:
        timetag = time.time() + timetag
    bundle = osc_bundle_builder.OscBundleBuilder(timetag)
    msg = build_message(msg_addr, msg_args)
    bundle.add_content(msg)
    bundle = bundle.build()
    return bundle


def build_message(msg_addr, msg_args):
    """Builds pythonsosc OSC message

    Arguments:
            msg_addr {str} -- SuperCollider address
                              E.g. '/s_new'
            msg_args {list} -- List of arguments to add
                               to message

    Returns:
        OscMessage -- Message ready to be sent
    """

    if msg_args is None:
            msg_args = []

    if not msg_addr.startswith('/'):
        msg_addr = '/' + msg_addr

    builder = osc_message_builder.OscMessageBuilder(address=msg_addr)
    if not hasattr(msg_args, '__iter__') or isinstance(msg_args, (str, bytes)):
        msg_args = [msg_args]
    for msg_arg in msg_args:
        builder.add_arg(msg_arg)
    msg = builder.build()
    return msg

class AddressQueue():

    def __init__(self, address, process=None):
        self.address = address
        self.process = process
        self.queue = Queue()
        
    def _put(self, address, *args):
        if self.process:
            args = self.process(args)
        else:
            if len(args) == 1:
                args = args[0]
        self.queue.put(args)

    @property
    def _map_values(self):
        return self.address, self._put

    def get(self, block=True, timeout=5):
        try:
            item = self.queue.get(block=True, timeout=timeout)
            self.queue.task_done()
            return item
        except Empty:
            raise Empty  # ToDo What do we want here?

    def show(self):
        print(list(self.queue.queue))



def process_return(value):
    value = value[0]
    if type(value) == bytes:
        value = parse_sclang_blob(value)
    return value


class OscCommunication():
    """Class to send and receive messages OSC messages via UDP
    from and to sclang and scsynth
    """

    def __init__(self, server_ip='127.0.0.1', server_port=OSCCOM_DEFAULT_PORT,
                 sclang_ip='127.0.0.1', sclang_port=SCLANG_DEFAULT_PORT,
                 scsynth_ip='127.0.0.1', scsynth_port=SCSYNTH_DEFAULT_PORT):
        print("Starting osc communication...")

        # set SuperCollider addresses
        self.set_sclang(sclang_ip, sclang_port)
        self.set_scsynth(scsynth_ip, scsynth_port)

        # start server
        server_dispatcher = dispatcher.Dispatcher()
        while True:
            try:
                self.server = osc_server.ThreadingOSCUDPServer(
                    (server_ip, server_port), server_dispatcher)
                print("This sc3nb sc instance is at port: {}"
                      .format(server_port))
                break
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    server_port += 1

        # init queues for msg pairs
        self._init_msgs()
        self.msg_queues = {}
        self.update_msg_queues()

        # init special msg queues
        self.returns = AddressQueue("/return", process_return)
        server_dispatcher.map(*self.returns._map_values)
        
        self.dones = AddressQueue("/done")
        server_dispatcher.map(*self.dones._map_values)
        
        # set logging handlers
        server_dispatcher.map("/fail", self._warn, needs_reply_address=True)
        server_dispatcher.map("/*", self._log, needs_reply_address=True)


        self.server_thread = threading.Thread(
            target=self.server.serve_forever)
        self.server_thread.start()

        print("Done.")

    def update_msg_queues(self, new_msg_pairs=None):
        if new_msg_pairs:
            self._msgs_pairs.update(new_msg_pairs)
        for msg_addr, response_addr in self._msgs_pairs.items():
            if msg_addr not in self.msg_queues:
                addr_queue = AddressQueue(response_addr)
                self.server.dispatcher.map(*addr_queue._map_values)
                self.msg_queues[msg_addr] = addr_queue

    def _check_sender(self, sender):
        if sender == self.sclang_address:
            sender = "sclang"
        elif sender == self.scsynth_address:
            sender = "scsynth"
        return sender

    def _log(self, sender, *args):
        logging.info("OSC_COM: osc msg received from {}: {}"
                     .format(self._check_sender(sender), args))

    def _warn(self, sender, *args):
        logging.warn("OSC_COM: Error from {}:\n {} {}"
                     .format(self._check_sender(sender), args[2], args[1]))

    def set_sclang(self, sclang_ip='127.0.0.1',
                   sclang_port=SCLANG_DEFAULT_PORT):
        self.sclang_address = (sclang_ip, sclang_port)

    def set_scsynth(self, scsynth_ip='127.0.0.1',
                    scsynth_port=SCSYNTH_DEFAULT_PORT):
        self.scsynth_address = (scsynth_ip, scsynth_port)

    def get_connection_info(self, print_info=True):
        if print_info:
            print("This server {}\nsclang {}\nscsynth {}"
                  .format(self.server.server_address,
                          self.sclang_address, self.scsynth_address))
        return (self.server.server_address,
                self.sclang_address, self.scsynth_address)

    def send(self, content, sclang):
        """Sends OSC message or bundle to sclang or scsnyth

        Arguments:
            content {OscMessage|OscBundle} -- Message or bundle
                                          to be sent
            sclang {bool} -- if True sends msg to sclang
                             else sends msg to scsynth
        """

        if sclang:
            self.server.socket.sendto(content.dgram, (self.sclang_address))
        else:
            self.server.socket.sendto(content.dgram, (self.scsynth_address))

    def sync(self, timeout=5):
        sync_id = randint(1000, 9999)  
        timeout_end = time.time() + timeout
        while True: 
            if sync_id == self.msg("/sync", sync_id, timeout=timeout):
                break
            if time.time() >= timeout_end:
                raise TimeoutError('timeout when waiting for /synced from server')

    def msg(self, msg_addr, msg_args=None, sclang=False, sync=True, timeout=5):
        """Sends OSC message over UDP to either sclang or scsynth

        Arguments:
            msg_addr {str} -- SuperCollider address
                              E.g. '/s_new'

        Keyword Arguments:
            msg_args {list} -- List of arguments to add to
                               message (default: {None})
            sclang {bool} -- if True send message to sclang,
                             otherwise send to scsynth
                             (default: {False})
            sync {bool} -- if True send message and wait for sync or response
                           otherwise send the message and return directly
            timeout {int} -- timeout for sync and response 
        """

        msg = build_message(msg_addr, msg_args)
        self.send(msg, sclang)
        logging.info("send {} ".format(msg.address))
        logging.debug("msg.params {}".format(msg.params))
        if msg.address in self._async_msgs and sync:
            self.sync(timeout=timeout)
        elif msg.address in self._msgs_pairs and sync:
            return self.msg_queues[msg.address].get(timeout=timeout)
            
    def bundle(self, timetag, msg_addr, msg_args=None, sclang=False):
        """Sends OSC bundle over UDP to either sclang or scsynth

        Arguments:
            timetag {int} -- Time at which bundle content
                             should be executed, either in
                             absolute or relative time
            msg_addr {str} -- SuperCollider address
                              E.g. '/s_new'

        Keyword Arguments:
            msg_args {list} -- List of arguments to add to
                               message (default: {None})
            sclang {bool} -- if True send message to sclang,
                             otherwise send to scsynth
                             (default: {False})
        """

        bundle = build_bundle(timetag, msg_addr, msg_args)
        self.send(bundle, sclang)

    def exit(self):
        self.server.shutdown()

    def _init_msgs(self):
        self._async_msgs = [
            "/quit",    # Master
            "/notify",
            "/d_recv",  # Synth Def load SynthDefs
            "/d_load",
            "/d_loadDir",
            "/b_alloc",  # Buffer Commands
            "/b_allocRead",
            "/b_allocReadChannel",
            "/b_read",
            "/b_readChannel",
            "/b_write",
            "/b_free",
            "/b_zero",
            "/b_gen",
            "/b_close"
        ]

        self._msgs_pairs = {
            # Master
            "/status": "/status.reply",     # [unused, #UGen, #Synth, #Group, #SynthDef, avgCPU, maxCPU, nomSampleRate, actualSample rate]
            "/sync": "/synced",             # [id]
            "/version": "/version.reply",   # [name, major ver, minor ver, patch, branch, commit]
            # Synth Commands
            "/s_get": "/n_set",             # nodeID, param, value, ...
            "/s_getn": "/n_setn",           # nodeID, number of controls to change,
            # Group Commands
            "/g_queryTree": "/g_queryTree.reply",
            # Node Commands
            "/n_query": "/n_info",          # for each nodeID passed to all clients that registered
            # Buffer Commands
            "/b_query":  "/b_info",         # /b_info, bufNum, #frames, #channels, sampleRate"
            "/b_get":  "/b_set",            # /b_set, bufNum, s_idx, s_val, ..."
            "/b_getn":  "/b_setn",          # /b_setn, bufNum, s_start_idx, #changes, s_val, ..."
            # Control Bus Commands
            "/c_get":  "/c_set",            # /c_set, bus_idx, control_val,..."
            "/c_getn":  "/c_setn"
        }
