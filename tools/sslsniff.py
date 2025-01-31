#!/usr/bin/python
#
# sslsniff  Captures data on read/recv or write/send functions of OpenSSL,
#           GnuTLS and NSS
#           For Linux, uses BCC, eBPF.
#
# USAGE: sslsniff.py [-h] [-p PID] [-c COMM] [-o] [-g] [-d]
#
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 12-Aug-2016    Adrian Lopez   Created this.
# 13-Aug-2016    Mark Drayton   Fix SSL_Read
# 17-Aug-2016    Adrian Lopez   Capture GnuTLS and add options
#

from __future__ import print_function
from bcc import BPF
import argparse
import binascii
import textwrap

# arguments
examples = """examples:
    ./sslsniff              # sniff OpenSSL and GnuTLS functions
    ./sslsniff -p 181       # sniff PID 181 only
    ./sslsniff -c curl      # sniff curl command only
    ./sslsniff --no-openssl # don't show OpenSSL calls
    ./sslsniff --no-gnutls  # don't show GnuTLS calls
    ./sslsniff --no-nss     # don't show NSS calls
    ./sslsniff --hexdump    # show data as hex instead of trying
                            # to decode it as UTF-8
"""
parser = argparse.ArgumentParser(
    description="Sniff SSL data",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-p", "--pid", type=int, help="sniff this PID only.")
parser.add_argument("-c", "--comm",
                    help="sniff only commands matching string.")
parser.add_argument("-o", "--no-openssl", action="store_false", dest="openssl",
                    help="do not show OpenSSL calls.")
parser.add_argument("-g", "--no-gnutls", action="store_false", dest="gnutls",
                    help="do not show GnuTLS calls.")
parser.add_argument("-n", "--no-nss", action="store_false", dest="nss",
                    help="do not show NSS calls.")
parser.add_argument('-d', '--debug', dest='debug', action='count', default=0,
                    help='debug mode.')
parser.add_argument("--ebpf", action="store_true",
                    help=argparse.SUPPRESS)
parser.add_argument("--hexdump", action="store_true", dest="hexdump",
                    help="show data as hexdump instead of trying to decode it as UTF-8")
args = parser.parse_args()


prog = """
#include <linux/ptrace.h>
#include <linux/sched.h>        /* For TASK_COMM_LEN */

struct probe_SSL_data_t {
        u64 timestamp_ns;
        u32 pid;
        char comm[TASK_COMM_LEN];
        char v0[MAX_BUF_SIZE];
        u32 len;
};

BPF_PERCPU_ARRAY(data_map, struct probe_SSL_data_t, 1);

BPF_PERF_OUTPUT(perf_SSL_write);

int probe_SSL_write(struct pt_regs *ctx, void *ssl, void *buf, u32 num) {
        u64 pid_tgid = bpf_get_current_pid_tgid();
        u32 pid = pid_tgid >> 32;

        FILTER

        u32 zero = 0;
        struct probe_SSL_data_t *__data = data_map.lookup(&zero);

        if ( !__data ) {
                return 0;
        }

        __data->timestamp_ns = bpf_ktime_get_ns();
        __data->pid = pid;
        __data->len = num;

        if ( num == 4294967295 ) {
                return 0;
        }

        u32 size = ( num > MAX_BUF_SIZE - 1 ) ? (MAX_BUF_SIZE-1) : num;

        bpf_get_current_comm(&__data->comm, sizeof(__data->comm));

        if ( buf != 0) {
                bpf_probe_read_user(&__data->v0, size, buf);
        }

        __data->v0[size] = 0;

        perf_SSL_write.perf_submit(ctx, __data, sizeof(*__data));
        return 0;
}

BPF_PERF_OUTPUT(perf_SSL_read);

BPF_HASH(bufs, u32, u64);

int probe_SSL_read_enter(struct pt_regs *ctx, void *ssl, void *buf, int num) {
        u64 pid_tgid = bpf_get_current_pid_tgid();
        u32 pid = pid_tgid >> 32;
        u32 tid = (u32)pid_tgid;

        FILTER

        bufs.update(&tid, (u64*)&buf);
        return 0;
}

int probe_SSL_read_exit(struct pt_regs *ctx, void *ssl, void *buf, int num) {
        u64 pid_tgid = bpf_get_current_pid_tgid();
        u32 pid = pid_tgid >> 32;
        u32 tid = (u32)pid_tgid;

        FILTER

        u64 *bufp = bufs.lookup(&tid);
        if (bufp == 0) {
                return 0;
        }

        u32 zero = 0;
        struct probe_SSL_data_t *__data = data_map.lookup(&zero);

        if ( !__data ) {
                return 0;
        }

        __data->timestamp_ns = bpf_ktime_get_ns();
        __data->pid = pid;
        __data->len = PT_REGS_RC(ctx);

        if ( __data->len == 4294967295 ) {
                return 0;
        }

        bpf_get_current_comm(&__data->comm, sizeof(__data->comm));

        u32 size = ( __data->len > MAX_BUF_SIZE - 1 ) ? (MAX_BUF_SIZE - 1): __data->len;

        if (bufp != 0) {
                bpf_probe_read_user(&__data->v0, size, (char *)*bufp);
        }

        __data->v0[size] = 0;

        bufs.delete(&tid);

        perf_SSL_read.perf_submit(ctx, __data, sizeof(*__data));
        return 0;
}
"""

# define output data structure in Python
TASK_COMM_LEN = 16  # linux/sched.h
MAX_BUF_SIZE = 1024 * 8


if args.pid:
    prog = prog.replace('FILTER', 'if (pid != %d) { return 0; }' % args.pid)
else:
    prog = prog.replace('FILTER', '')

prog = prog.replace('MAX_BUF_SIZE', '%d' % MAX_BUF_SIZE)

if args.debug or args.ebpf:
    print(prog)
    if args.ebpf:
        exit()


b = BPF(text=prog)

# It looks like SSL_read's arguments aren't available in a return probe so you
# need to stash the buffer address in a map on the function entry and read it
# on its exit (Mark Drayton)
#
if args.openssl:
    b.attach_uprobe(name="ssl", sym="SSL_write", fn_name="probe_SSL_write",
                    pid=args.pid or -1)
    b.attach_uprobe(name="ssl", sym="SSL_read", fn_name="probe_SSL_read_enter",
                    pid=args.pid or -1)
    b.attach_uretprobe(name="ssl", sym="SSL_read",
                       fn_name="probe_SSL_read_exit", pid=args.pid or -1)

if args.gnutls:
    b.attach_uprobe(name="gnutls", sym="gnutls_record_send",
                    fn_name="probe_SSL_write", pid=args.pid or -1)
    b.attach_uprobe(name="gnutls", sym="gnutls_record_recv",
                    fn_name="probe_SSL_read_enter", pid=args.pid or -1)
    b.attach_uretprobe(name="gnutls", sym="gnutls_record_recv",
                       fn_name="probe_SSL_read_exit", pid=args.pid or -1)

if args.nss:
    b.attach_uprobe(name="nspr4", sym="PR_Write", fn_name="probe_SSL_write",
                    pid=args.pid or -1)
    b.attach_uprobe(name="nspr4", sym="PR_Send", fn_name="probe_SSL_write",
                    pid=args.pid or -1)
    b.attach_uprobe(name="nspr4", sym="PR_Read", fn_name="probe_SSL_read_enter",
                    pid=args.pid or -1)
    b.attach_uretprobe(name="nspr4", sym="PR_Read",
                       fn_name="probe_SSL_read_exit", pid=args.pid or -1)
    b.attach_uprobe(name="nspr4", sym="PR_Recv", fn_name="probe_SSL_read_enter",
                    pid=args.pid or -1)
    b.attach_uretprobe(name="nspr4", sym="PR_Recv",
                       fn_name="probe_SSL_read_exit", pid=args.pid or -1)


# header
print("%-12s %-18s %-16s %-7s %-6s" % ("FUNC", "TIME(s)", "COMM", "PID",
                                       "LEN"))

# process event
start = 0


def print_event_write(cpu, data, size):
    print_event(cpu, data, size, "WRITE/SEND", "perf_SSL_write")


def print_event_read(cpu, data, size):
    print_event(cpu, data, size, "READ/RECV", "perf_SSL_read")


def print_event(cpu, data, size, rw, evt):
    global start
    event = b[evt].event(data)

    # Filter events by command
    if args.comm:
        if not args.comm == event.comm.decode('utf-8', 'replace'):
            return

    if start == 0:
        start = event.timestamp_ns
    time_s = (float(event.timestamp_ns - start)) / 1000000000

    s_mark = "-" * 5 + " DATA " + "-" * 5

    e_mark = "-" * 5 + " END DATA " + "-" * 5

    truncated_bytes = event.len - MAX_BUF_SIZE
    if truncated_bytes > 0:
        e_mark = "-" * 5 + " END DATA (TRUNCATED, " + str(truncated_bytes) + \
                " bytes lost) " + "-" * 5

    fmt = "%-12s %-18.9f %-16s %-7d %-6d\n%s\n%s\n%s\n\n"
    if args.hexdump:
        unwrapped_data = binascii.hexlify(event.v0)
        data = textwrap.fill(unwrapped_data.decode('utf-8', 'replace'), width=32)
    else:
        data = event.v0.decode('utf-8', 'replace')
    print(fmt % (rw, time_s, event.comm.decode('utf-8', 'replace'),
                 event.pid, event.len, s_mark, data, e_mark))


b["perf_SSL_write"].open_perf_buffer(print_event_write)
b["perf_SSL_read"].open_perf_buffer(print_event_read)
while 1:
    try:
        b.perf_buffer_poll()
    except KeyboardInterrupt:
        exit()
