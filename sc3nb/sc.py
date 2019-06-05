"""Contains classes enabling the easy communication with SuperCollider
within jupyter notebooks
"""

import os
import re
import subprocess
import sys
import threading
import time
from queue import Empty, Queue

import numpy as np
from IPython import get_ipython
from IPython.core.magic import Magics, cell_magic, line_magic, magics_class

from .tools import (convert_to_sc, find_executable, parse_pyvars,
                    remove_comments, replace_vars)
from .osc_communication import (OscCommunication, SCLANG_DEFAULT_PORT,
                                build_bundle, build_message)
from .buffer import Buffer

if os.name == 'posix':
    import fcntl

ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')


class SC():
    """SC is a class to start SuperCollider language as subprocess
    and control it via a pipe. Communication with scsynth is handled
    by OSC messages via. Jupyter magic commands allow for simple
    execution of SuperCollider code within jupyter notebooks.
    (c) 2016-19 thermann@techfak.uni-bielefeld.de

    Keyword Arguments:
        sclangpath {str} -- Path to sclang
                            (default: {None})

    Raises:
        NotImplementedError -- Raised if
                               unsupported OS is found
    """

    sc = None

    def __init__(self, sclangpath=None, console_logging=True):

        SC.sc = self

        self.console_logging = console_logging

        if sys.platform == "linux" or sys.platform == "linux2":
            self.terminal_symbol = 'sc3>'
            self.__read_loop = self.__read_loop_unix
        elif sys.platform == "darwin":
            self.terminal_symbol = 'sc3>'
            self.__read_loop = self.__read_loop_unix
        elif sys.platform == "win32":
            self.terminal_symbol = '->'
            self.__read_loop = self.__read_loop_windows
        else:
            raise NotImplementedError('Unsupported OS {}'.format(sys.platform))

        # toggle variable to know if server has been started when exiting
        self.server = False

        # add sclang path to if environment if not already found
        if sclangpath is not None:
            sclangpath = os.path.split(sclangpath)
            if 'sclang' in sclangpath[1]:
                sclangpath = sclangpath[0]
            else:
                sclangpath = os.path.join(*sclangpath)
            if sclangpath not in os.environ['PATH']:
                os.environ['PATH'] += os.pathsep + sclangpath
        else:
            sclangpath = ''

        # find sclang in environment
        sclangpath = find_executable('sclang', path=sclangpath)

        self.sc_end_marker_prog = '("finished"+"booting").postln;'
        # hack to print 'finished booting' without having 'finished booting'
        # in the code needed for MacOS since sclang echos input.
        # In consequence the read_loop_unix() waiting for the
        # 'finished booting' string returns too early...
        # TODO: open issue for sc3 github
        #       asking for 'no echo' command line option macos sclang

        # sclang subprocess
        self.scp = subprocess.Popen(
            args=[sclangpath],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)

        self.rec_node_id = -1  # i.e. not valid
        self.rec_bufnum = -1

        self.scp_queue = Queue()

        scp_thread = threading.Thread(target=self.__read_loop, args=(
            self.scp.stdout, self.scp_queue))
        scp_thread.setDaemon(True)
        scp_thread.start()

        print('Starting sclang...')

        self.__scpout_read(timeout=10, terminal='Welcome to SuperCollider')

        print('Done.')

        self.osc = OscCommunication()

        print('Registering UDP callback...')

        self.cmd(r'''
            r = r ? ();
            r.callback = { arg code, ip, port;
                var result = code.interpret;
                var addr = NetAddr.new(ip, port);
                var prependSize = { arg elem;
                    if (elem.class == Array){
                        elem = [elem.size] ++ elem.collect(prependSize);
                    }{
                        elem;
                    };
                };
                result = prependSize.value(result);
                addr.sendMsg("/return", result);
                result;  // result should be returned
            };
            ''')

        self.__scpout_read(terminal='a Function')

        print('Done.')

        sclang_port = self.cmdg('NetAddr.langPort')
        if sclang_port != SCLANG_DEFAULT_PORT:
            self.osc.set_sclang(sclang_port=sclang_port)
            print('Sclang started on non default port: {}'.format(sclang_port))

        # counter for nextNodeID
        self.num_IDs = 0
        self.num_buffer_IDs = 0

        # clear output buffer
        self.__scpout_empty()

    def cmd(self, cmdstr, pyvars=None,
            verbose=False, discard_output=True,
            get_result=False, timeout=1):
        """Sends code to SuperCollider (sclang)

        Arguments:
            cmdstr {str} -- SuperCollider code

        Keyword Arguments:
            pyvars {dict} -- Dictionary of name and value pairs
                             of python variables that can be
                             injected via ^name
                             (default: {None})
            verbose {bool} -- if True print output
                              (default: {False})
            discard_output {bool} -- if True clear output
                                     buffer before passing
                                     command
                                     (default: {True})
            get_result {bool} -- if True receive and return
                                 the evaluation result
                                 from sclang
                                 (default: {False})
            timeout {int} -- Timeout time for receiving data
                             (default: {1})

        Returns:
            {*} -- if get_result=True,
                   Output from SuperCollider code,
                   not all SC types supported.
                   When type is not understood this
                   will return the data gram from the
                   OSC packet.
        """
        if pyvars is None:
            pyvars = parse_pyvars(cmdstr)
        cmdstr = replace_vars(cmdstr, pyvars)

        # cleanup command string
        cmdstr = remove_comments(cmdstr)
        cmdstr = re.sub(r'\s+', ' ', cmdstr).strip()

        if get_result:
            # escape " and \ in our SuperCollider string literal
            inner_cmdstr_escapes = str.maketrans(
                {ord('\\'): r'\\', ord('"'): r'\"'})
            inner_cmdstr = cmdstr.translate(inner_cmdstr_escapes)
            # wrap the command string with our callback function
            cmdstr = r"""r['callback'].value("{0}", "{1}", {2});""".format(
                inner_cmdstr, *self.osc.server.server_address)

        if verbose and discard_output:
                self.__scpout_empty()  # clean all past outputs

        # write command to sclang pipe \f
        if cmdstr and cmdstr[-1] != ';':
            cmdstr += ';'
        self.scp.stdin.write(bytearray(cmdstr + '\n\f', 'utf-8'))
        self.scp.stdin.flush()

        return_val = None

        if get_result:
            try:
                return_val = self.osc.returns.get(block=True, timeout=timeout)
            except Empty:
                print("SCLANG ERROR:")
                print(self.__scpout_read(terminal=self.terminal_symbol))
                raise ChildProcessError("unable to receive result from sclang")

        if verbose:
            # get output after current command
            out = self.__scpout_read(terminal=self.terminal_symbol)
            if sys.platform != 'win32':
                out = ansi_escape.sub('', out)  # remove ansi chars
                out = out.replace('sc3>', '')  # remove prompt
                out = out[out.find(';\n') + 2:]  # skip cmdstr echo
            out = out.strip()
            print(out)
            if not get_result:
                return_val = out

        return return_val

    def cmdv(self, cmdstr, **kwargs):
        """Sends code to SuperCollider (sclang)
           and prints output

        Arguments:
            cmdstr {str} -- SuperCollider code

        Keyword Arguments:
            pyvars {dict} -- Dictionary of name and value pairs
                             of python variables that can be
                             injected via ^name
                             (default: {None})
            discard_output {bool} -- if True clear output
                                     buffer before passing
                                     command
                                     (default: {True})
            get_result {bool} -- if True receive and return
                                 the evaluation result
                                 from sclang
                                 (default: {False})
            timeout {int} -- Timeout time for receiving data
                             (default: {1})

        Returns:
            {*} -- if get_result=True,
                   Output from SuperCollider code,
                   not all SC types supported.
                   When type is not understood this
                   will return the data gram from the
                   OSC packet.
        """
        if kwargs.get("pyvars", None) is None:
            kwargs["pyvars"] = parse_pyvars(cmdstr)
        return self.cmd(cmdstr, verbose=True, **kwargs)

    def cmdg(self, cmdstr, **kwargs):
        """Sends code to SuperCollider (sclang)
           and receives and returns the evaluation result

        Arguments:
            cmdstr {str} -- SuperCollider code

        Keyword Arguments:
            pyvars {dict} -- Dictionary of name and value pairs
                             of python variables that can be
                             injected via ^name
                             (default: {None})
            verbose {bool} -- if True print output
                              (default: {False})
            discard_output {bool} -- if True clear output
                                     buffer before passing
                                     command
                                     (default: {True})
            timeout {int} -- Timeout time for receiving data
                             (default: {1})

        Returns:
            {*} -- if get_result=True,
                   Output from SuperCollider code,
                   not all SC types supported.
                   When type is not understood this
                   will return the data gram from the
                   OSC packet.
        """
        if kwargs.get("pyvars", None) is None:
            kwargs["pyvars"] = parse_pyvars(cmdstr)
        return self.cmd(cmdstr, get_result=True, **kwargs)

    def boot(self):
        """Boots SuperCollider server
        """
        print('Booting server...')

        self.cmd('s.boot.doWhenBooted({' + self.sc_end_marker_prog + '})')

        self.server = True

        self.__scpout_read(terminal='finished booting')

        print('Done.')

    def free_all(self):
        """Frees all SuperCollider synths (executes s.freeAll)
        """

        self.cmd("s.freeAll")

    def nextNodeID(self):
        """Returns the next nodeID, starting at 10000, not clientID based
        """

        self.num_IDs += 1
        nodeID = self.num_IDs + 10000
        return nodeID

    def nextBufferID(self):
        """Returns the next bufferID, starting at 100, not clientID based
        """

        self.num_buffer_IDs += 1
        return self.num_buffer_IDs + 100

    def exit(self):
        """Closes SuperCollider and shuts down server
        """

        if SC.sc == self:
            if self.server:
                self.__s_quit()
        self.osc.exit()
        self.scp.kill()

    def boot_with_blip(self):
        """Boots SuperCollider server with audio feedback
        """

        print('Booting server...')

        self.server = True

        # make sure SC is booted and knows this synths:
        self.cmd(r"""
            Server.default = s = Server.local;
            s.boot.doWhenBooted(
            { Routine({
            /* synth definitions *********************************/
            "load synth definitions".postln;
            SynthDef("s1",
                { | freq=400, dur=0.4, att=0.01, amp=0.3, num=4, pan=0 |
                    Out.ar(0, Pan2.ar(Blip.ar(freq,  num)*
                    EnvGen.kr(Env.perc(att, dur, 1, -2), doneAction: 2),
                    pan, amp))
                }).add();
            SynthDef("s2",
                { | freq=400, amp=0.3, num=4, pan=0, lg=0.1 |
                    Out.ar(0, Pan2.ar(Blip.ar(freq.lag(lg),  num),
                              pan.lag(lg), amp.lag(lg)))
                }).add();
            SynthDef("record-2ch",
                { | bufnum |
                    DiskOut.ar(bufnum, In.ar(0, 2));
                }).add();
            SynthDef("record-1ch",
                { | bufnum |
                    DiskOut.ar(bufnum, In.ar(0, 1));
                }).add();
            SynthDef("pb-1ch",
                { |out=0, bufnum=0, rate=1, loop=0, pan=0, amp=0.3 |
                    Out.ar(out,
                        PlayBuf.ar(1, bufnum, rate*BufRateScale.kr(bufnum),
                                   loop: loop,
                                   doneAction: 2)!2
                )
            }).add();
            SynthDef("pb-2ch",
                { |out=0, bufnum=0, rate=1, loop=0, pan=0, amp=0.3 |
                    Out.ar(out,
                        PlayBuf.ar(2, bufnum, rate*BufRateScale.kr(bufnum),
                                   loop: loop,
                                   doneAction: 2)!2
                )
            }).add();
            s.sync;
            /* test signals ****************************************/
            "create test signals".postln;
            Synth.new(\s1, [\freq, 500, \dur, 0.1, \num, 1]);
            0.2.wait;
            x = Synth.new(\s2, [\freq, 1000, \amp, 0.05, \num, 2]);
            0.1.wait; x.free;""" + self.sc_end_marker_prog + r"""}).play} , 1000);
        """)

        self.__scpout_read(timeout=10, terminal='finished booting')

        print('Done.')

    def msg(self, msg_addr, msg_args=None, sclang=False):
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

        return self.osc.msg(msg_addr, msg_args, sclang)

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

        return self.osc.bundle(timetag, msg_addr, msg_args, sclang)

    def prepare_for_record(self, onset=0, wavpath="record.wav",
                           bufnum=99, nr_channels=2, rec_header="wav",
                           rec_format="int16"):
        """Setup recording via scsynth

        Keyword Arguments:
            onset {int} -- Bundle timetag (default: {0})
            wavpath {str} -- Save file path
                             (default: {"record.wav"})
            bufnum {int} -- Buffer number (default: {99})
            nr_channels {int} -- Number of channels
                                 (default: {2})
            rec_header {str} -- File format
                                (default: {"wav"})
            rec_format {str} -- Recording resolution
                                (default: {"int16"})
        """

        self.rec_bufnum = bufnum
        self.bundle(
            onset,
            "/b_alloc",
            [self.rec_bufnum, 65536, nr_channels])
        self.bundle(
            onset,
            "/b_write",
            [self.rec_bufnum, wavpath, rec_header, rec_format, 0, 0, 1])

    def record(self, onset=0, node_id=2001, nr_channels=2):
        """Start recording

        Keyword Arguments:
            onset {int} -- Bundle timetag (default: {0})
            node_id {int} -- SuperCollider Node id
                             (default: {2001})
        """

        self.rec_node_id = node_id
        if nr_channels == 1:
            self.bundle(
                onset,
                "/s_new",
                ["record-1ch",
                 self.rec_node_id, 1, 0, "bufnum", self.rec_bufnum])
        else:
            self.bundle(
                onset,
                "/s_new",
                ["record-2ch",
                 self.rec_node_id, 1, 0, "bufnum", self.rec_bufnum])
            # action = 1 = addtotail

    def stop_recording(self, onset=0):
        """Stop recording

        Keyword Arguments:
            onset {int} -- Bundle timetag (default: {0})
        """

        self.bundle(onset, "/n_free", [self.rec_node_id])
        self.bundle(onset, "/b_close", [self.rec_bufnum])
        self.bundle(onset, "/b_free", [self.rec_bufnum])

    def midi_ctrl_synth(self, synthname='syn'):
        """Set up MIDI control synth

        Keyword Arguments:
            synthname {str} -- Name of synth
                               (default: {'syn'})
        """

        self.cmd(r"""
            MIDIIn.connectAll;
            n.free;
            n = MIDIFunc.noteOn(
                { | level, pitch |
                    var amp = ((level-128)/8).dbamp;
                    Synth.new(^synthname, [\freq, pitch.midicps, \amp, amp]);
                    [pitch, amp].postln
                });
            """, pyvars={"synthname": synthname})

    def midi_ctrl_free(self):
        """Free MIDI control synth
        """

        self.cmd("n.free")

    def midi_gate_synth(self, synthname='syn'):
        """Set up MIDI gate synth

        Keyword Arguments:
            synthname {str} -- Name of synth
                               (default: {'syn'})
        """

        self.cmd(r"""
            MIDIIn.connectAll;
            q = q ? ();
            // q.on.free;
            // q.off.free;
            // array has one slot per possible MIDI note
            q.notes = Array.newClear(128);
            q.on = MIDIFunc.noteOn({ |veloc, num, chan, src|
                q.notes[num] = Synth.new(
                    ^synthname,
                    [\freq, num.midicps, \amp, veloc * 0.00315]);
            });
            q.off = MIDIFunc.noteOff({ |veloc, num, chan, src|
                q.notes[num].release;
            });
            q.freeMIDI = { q.on.free; q.off.free; };
            """, pyvars={"synthname": synthname})

    def midi_gate_free(self):
        """Free MIDI gate synth
        """
        self.cmd("q.on.free; q.off.free")

    def __del__(self):
        '''Handles clean deletion of object'''
        self.exit()

    def __s_quit(self):
        '''Quits the server'''
        print('Shutting down server...')

        self.cmd('s.quit')

        self.__scpout_read(terminal='RESULT = 0')

        self.server = False

        print('Done.')

    def __scpout_read(self, timeout=1, terminal=None):
        '''Reads first sc output from output queue'''
        timeout = time.time() + timeout
        out = ''
        terminal_found = False
        while True:
            if time.time() >= timeout:
                raise TimeoutError('timeout when reading SC stdout')
            try:
                retbytes = self.scp_queue.get_nowait()
                if retbytes is not None:
                    out += retbytes.decode()
                    if re.search(terminal, out):
                        terminal_found = True
            except Empty:
                if terminal and not terminal_found:
                    pass
                else:
                    return out
            time.sleep(0.001)

    def __scpout_empty(self):
        '''Empties sc output queue'''
        while True:
            try:
                self.scp_queue.get_nowait()
            except Empty:
                return

    def __read_loop_windows(self, output, queue):
        for line in iter(output.readline, b''):
            queue.put(line)
            if self.console_logging:
                # print to jupyter console...
                os.write(1, line)

    def __read_loop_unix(self, output, queue):
        file_descriptor = output.fileno()
        file_flags = fcntl.fcntl(file_descriptor, fcntl.F_GETFL)
        fcntl.fcntl(output, fcntl.F_SETFL, file_flags | os.O_NONBLOCK)
        while True:
            try:
                out = output.read()
                if out:
                    queue.put(out)
                    if self.console_logging:
                        # to remove ansi chars
                        out = ansi_escape.sub('', out.decode())
                        # print to jupyter console...
                        os.write(1, out.encode())
            except EOFError:
                pass
            time.sleep(0.001)

    def Buffer(self, **kwargs):
        return Buffer(self, **kwargs)


def startup(boot=True, magic=True, **kwargs):
    """Starts SC, boots scsynth and registers magics

    Keyword Arguments:
        boot {bool} -- if True boot scsynth
                       (default: {True})
        magic {bool} -- if True register jupyter magics
                        (default: {True})

    Returns:
        SC -- Communicates with and controls SuperCollider
    """

    sc = SC(**kwargs)
    if boot:
        sc.boot_with_blip()
    sc.cmdv('\"sc3nb started\";')
    if magic:
        ip = get_ipython()
        if ip is not None:
            ip.register_magics(SC3Magics)
    return sc

# SC3 magics


@magics_class
class SC3Magics(Magics):
    """Jupyter magics for SC class
    """

    @cell_magic
    @line_magic
    def sc(self, line='', cell=None):
        """Execute SuperCollider code via magic

        Keyword Arguments:
            line {str} -- Line of SuperCollider code (default: {''})
            cell {str} -- Cell of SuperCollider code (default: {None})
        """

        if line:
            pyvars = self.__parse_pyvars(line)
            SC.sc.cmd(line, pyvars=pyvars)
        if cell:
            pyvars = self.__parse_pyvars(cell)
            SC.sc.cmd(cell, pyvars=pyvars)

    @cell_magic
    @line_magic
    def scv(self, line='', cell=None):
        """Execute SuperCollider code with verbose output

        Keyword Arguments:
            line {str} -- Line of SuperCollider code (default: {''})
            cell {str} -- Cell of SuperCollider code (default: {None})
        """

        if line:
            pyvars = self.__parse_pyvars(line)
            SC.sc.cmdv(line, pyvars=pyvars)
        if cell:
            pyvars = self.__parse_pyvars(cell)
            SC.sc.cmdv(cell, pyvars=pyvars)

    @cell_magic
    @line_magic
    def scg(self, line='', cell=None):
        """Execute SuperCollider code returning output

        Keyword Arguments:
            line {str} -- Line of SuperCollider code (default: {''})
            cell {str} -- Cell of SuperCollider code (default: {None})

        Returns:
            {*} -- Output from SuperCollider code, not
                   all SC types supported, see
                   pythonosc.osc_message.Message for list
                   of supported types
        """

        if line:
            pyvars = self.__parse_pyvars(line)
            return SC.sc.cmdg(line, pyvars=pyvars)
        if cell:
            pyvars = self.__parse_pyvars(cell)
            return SC.sc.cmdg(cell, pyvars=pyvars)

    @cell_magic
    @line_magic
    def scgv(self, line='', cell=None):
        """Execute SuperCollider code returning output

        Keyword Arguments:
            line {str} -- Line of SuperCollider code (default: {''})
            cell {str} -- Cell of SuperCollider code (default: {None})

        Returns:
            {*} -- Output from SuperCollider code, not
                   all SC types supported, see
                   pythonosc.osc_message.Message for list
                   of supported types
        """

        if line:
            pyvars = self.__parse_pyvars(line)
            return SC.sc.cmdg(line, pyvars=pyvars, verbose=True)
        if cell:
            pyvars = self.__parse_pyvars(cell)
            return SC.sc.cmdg(cell, pyvars=pyvars, verbose=True)

    def __parse_pyvars(self, cmdstr):
        """Parses SuperCollider code grabbing python variables
        and their values
        """

        matches = re.findall(r'\s*\^[A-Za-z_]\w*\s*', cmdstr)

        pyvars = {match.split('^')[1].strip(): None for match in matches}

        user_ns = self.shell.user_ns

        for pyvar in pyvars:
            if pyvar in user_ns:
                pyvars[pyvar] = user_ns[pyvar]
            else:
                raise NameError('name \'{}\' is not defined'.format(pyvar))

        return pyvars


try:
    if sys.platform == "linux" or sys.platform == "linux2":
        get_ipython().run_cell_magic('javascript', '',
                                     '''Jupyter.keyboard_manager.command_shortcuts.add_shortcut(
                                        \'Ctrl-.\', {
                                        help : \'sc.cmd("s.freeAll")\',
                                        help_index : \'zz\',
                                        handler : function (event) {
                                            IPython.notebook.kernel.execute("sc.cmd(\'s.freeAll\')")
                                            return true;}
                                    });''')
    elif sys.platform == "darwin":
        get_ipython().run_cell_magic('javascript', '',
                                     '''Jupyter.keyboard_manager.command_shortcuts.add_shortcut(
                                        \'cmd-.\', {
                                        help : \'sc.cmd("s.freeAll")\',
                                        help_index : \'zz\',
                                        handler : function (event) {
                                            IPython.notebook.kernel.execute("sc.cmd(\'s.freeAll\')")
                                            return true;}
                                    });''')
    elif sys.platform == "win32":
        get_ipython().run_cell_magic('javascript', '',
                                     '''Jupyter.keyboard_manager.command_shortcuts.add_shortcut(
                                        \'Ctrl-.\', {
                                        help : \'sc.cmd("s.freeAll")\',
                                        help_index : \'zz\',
                                        handler : function (event) {
                                            IPython.notebook.kernel.execute("sc.cmd(\'s.freeAll\')")
                                            return true;}
                                    });''')
except AttributeError:
    pass
