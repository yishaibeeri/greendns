# -*- coding: utf-8 -*-
from __future__ import print_function
import logging
import argparse
import random
import dnslib
from pkg_resources import resource_filename
from greendns import session
from greendns import connection
from greendns import localnet
from greendns import handler_base
from greendns import cache

EXEMPT = 'wix'

class GreenDNSSession(session.Session):
    def __init__(self):
        super(GreenDNSSession, self).__init__()
        self.qtype = 0
        self.qname = ""
        self.is_poisoned = False
        self.local_result = None
        self.unpoisoned_result = None
        self.matrix = [[0, 0], [0, 0]]


class GreenDNSHandler(handler_base.HandlerBase):
    '''
    First filter poisoned ip with blocked iplist with -b argument.
    Second,
                                           | A record is local | A record is foreign
        local and poisoned dns server      |    a              |   b
        unpoisoned dns server              |    c              |   d

    From the matrix, we get the result as follows,
    ac: use local dns server result
    ad: use local dns server result
    bc: impossible. use unpoisoned dns server result
    bd: use unpoisoned dns server result
    '''
    def __init__(self):
        self.logger = logging.getLogger()
        self.cnet = None
        self.f_localroute = None
        self.f_blacklist = None
        self.using_rfc1918 = None
        self.lds = None
        self.rds = None
        self.cache_enabled = False
        self.cache = cache.Cache()
        self.local_servers = []
        self.unpoisoned_servers = []

        # Override IP
        self.override = None

    def add_arg(self, parser):
        parser.add_argument("--lds",
                            help="Specify local poisoned dns servers",
                            default="223.5.5.5:53,114.114.114.114:53")
        parser.add_argument("--rds",
                            help="Specify unpoisoned dns servers",
                            default="tcp:208.67.222.220:5353,9.9.9.9:9953")
        parser.add_argument("-f", "--localroute", dest="localroute",
                            type=argparse.FileType('r'),
                            default=resource_filename(__name__, 'data/localroute.txt'),
                            help="Specify local routes file")
        parser.add_argument("-b", "--blacklist", dest="blacklist",
                            type=argparse.FileType('r'),
                            default=resource_filename(__name__, 'data/iplist.txt'),
                            help="Specify ip blacklist file")
        parser.add_argument("--rfc1918", dest="rfc1918", action="store_true",
                            help="Specify if rfc1918 ip is local")
        parser.add_argument("--cache", dest="cache", action="store_true",
                            help="Specify if cache is enabled")
        parser.add_argument("--override", dest="override",
                            help="override IP address")

    def parse_arg(self, parser, remaining_argv):
        myargs = parser.parse_args(remaining_argv)
        self.f_localroute = myargs.localroute
        self.f_blacklist = myargs.blacklist
        self.using_rfc1918 = myargs.rfc1918
        self.cache_enabled = myargs.cache
        self.lds = myargs.lds
        self.rds = myargs.rds
        self.override = myargs.override

    def init(self, io_engine):
        self.cnet = localnet.LocalNet(self.f_localroute,
                                      self.f_blacklist,
                                      self.using_rfc1918)
        for l in self.lds.split(','):
            addr = connection.parse_addr(l)
            if addr is None:
                return []
            self.local_servers.append(addr)
        for r in self.rds.split(','):
            addr = connection.parse_addr(r)
            if addr is None:
                return []
            self.unpoisoned_servers.append(addr)

        if self.cache_enabled:
            io_engine.add_timer(False, 1, self.__decrease_ttl_one)

        self.logger.info("using local servers: %s", self.local_servers)
        self.logger.info("using unpoisoned servers: %s", self.unpoisoned_servers)
        return self.local_servers + self.unpoisoned_servers

    def new_session(self):
        return GreenDNSSession()

    def on_client_request(self, sess):
        is_continue, raw_resp = False, ""
        try:
            d = dnslib.DNSRecord.parse(sess.req_data)
        except Exception as e:
            self.logger.error("[sid=%d] parse request error, msg=%s, data=%s",
                              sess.sid, e, sess.req_data)
            return (is_continue, raw_resp)
        self.logger.debug("[sid=%d] request detail,\n%s", sess.sid, d)
        if not d.questions:
            return (is_continue, raw_resp)
        qtype = d.questions[0].qtype
        qname = str(d.questions[0].qname)
        tid = d.header.id
        self.logger.info("[sid=%d] received request, name=%s, type=%s, id=%d",
                         sess.sid, qname, dnslib.QTYPE.get(qtype), tid)

        if EXEMPT in qname:
            sess._exempt = True
        else:
            sess._exempt = False

        if self.cache_enabled:
            resp = self.cache.find((qname, qtype))
            if resp:
                self.__replace_id(resp.header, tid)
                if qtype == dnslib.QTYPE.A:
                    self.__shuffer_A(resp)
                self.logger.info("[sid=%d] cache hit", sess.sid)
                self.logger.debug("[sid=%d] response detail,\n%s", sess.sid, resp)
                return (is_continue, bytes(resp.pack()))
        sess.qtype, sess.qname = qtype, qname
        is_continue = True
        return (is_continue, raw_resp)

    def on_upstream_response(self, sess, addr):
        resp = None
        if sess.qtype == dnslib.QTYPE.A:
            resp = self.__handle_A(sess, addr)
        else:
            #using the first answer from local server for other qtype
            if addr in self.local_servers:
                self.logger.info("[sid=%d] %s:%s:%d answer used",
                                 sess.sid, addr[0], addr[1], addr[2])
                resp = self.__handle_other(sess, addr)
        if resp:
            if self.cache_enabled and resp.rr:
                for answer in resp.rr:
                    if answer.rtype == sess.qtype:
                        ttl = answer.ttl
                        self.cache.add((sess.qname, sess.qtype), resp, ttl)
                        self.logger.info(
                            "[sid=%d] add to cache, key=(%s, %s), ttl=%d",
                            sess.sid, sess.qname, dnslib.QTYPE.get(sess.qtype),
                            ttl)
                        break
            return bytes(resp.pack())
        return ""

    def __handle_other(self, sess, addr):
        data = sess.server_resps.get(addr)
        if not data:
            return None
        try:
            d = dnslib.DNSRecord.parse(data)
            self.logger.debug("[sid=%d] %s:%s:%d response detail,\n%s",
                              sess.sid, addr[0], addr[1], addr[2], d)
            return d
        except Exception as e:
            self.logger.error("[sid=%d] parse response error, msg=%s, data=%s",
                              sess.sid, e, data)
            return None

    def __handle_A(self, sess, addr):
        data = sess.server_resps.get(addr)
        if not data:
            return None
        try:
            d = dnslib.DNSRecord.parse(data)
        except Exception as e:
            self.logger.error("[sid=%d] parse response error, err=%s, data=%s",
                              sess.sid, e, data)
            return None
        str_ip = self.__parse_A(d, exempt=sess._exempt)
        self.logger.info("[sid=%d] %s:%s:%d answered ip=%s", sess.sid, addr[0], addr[1], addr[2], str_ip)
        self.logger.debug("[sid=%d] %s:%s:%d response detail,\n%s", sess.sid, addr[0], addr[1], addr[2], d)
        if self.cnet.is_in_blacklist(str_ip):
            self.logger.info("[sid=%d] ip %s is in blacklist", sess.sid, str_ip)
            sess.is_poisoned = True
            return None
        if addr in self.local_servers:
            if sess.local_result:
                return None
            sess.local_result = d
            if str_ip:
                if self.cnet.is_in_local(str_ip):
                    sess.matrix[0][0] = 1
                    self.logger.info(
                        "[sid=%d] local server %s:%s:%d returned local addr %s",
                        sess.sid, addr[0], addr[1], addr[2], str_ip)
                else:
                    sess.matrix[0][1] = 1
                    self.logger.info(
                        "[sid=%d] local server %s:%s:%d returned foreign addr %s",
                        sess.sid, addr[0], addr[1], addr[2], str_ip)
        elif addr in self.unpoisoned_servers:
            if sess.unpoisoned_result:
                return None
            sess.unpoisoned_result = d
        else:
            self.logger.warning(
                "[sid=%d] unexpected answer from unknown server", sess.sid)
            return None
        return self.__make_response(sess.sid,
                                    sess.local_result,
                                    sess.unpoisoned_result,
                                    sess.matrix,
                                    sess.is_poisoned)

    def __make_response(self, sid, local_result, unpoisoned_result, m, is_poisoned):
        # calculate
        resp = None
        if m[0][0]:
            resp = local_result
            self.logger.info("[sid=%d] using local result", sid)
        elif m[0][1] or is_poisoned:
            resp = unpoisoned_result
            self.logger.info("[sid=%d] using unpoisoned result", sid)
        elif local_result:
            # empty body, no IP
            resp = unpoisoned_result
            self.logger.info("[sid=%d] using unpoisoned result", sid)
        return resp

    def __parse_A(self, record, exempt=False):
        '''parse a proper A record'''
        str_ip = ""
        local_ip = ""
        for rr in record.rr:
            if rr.rtype == dnslib.QTYPE.A:
                if self.override and not exempt:
                    rr.rdata = dnslib.A(self.override)
                str_ip = str(rr.rdata)
                if self.cnet.is_in_local(str_ip):
                    local_ip = str_ip
        if local_ip:
            return local_ip
        return str_ip

    def __replace_id(self, header, new_tid):
        header.id = new_tid

    def __shuffer_A(self, resp):
        beg, end = -1, 0
        rr_A, rr_other = [], []
        for idx, rr in enumerate(resp.rr):
            if rr.rtype == dnslib.QTYPE.A:
                rr_A.append(rr)
                if beg < 0:
                    beg = idx
                else:
                    end = idx
            else:
                rr_other.append(rr)
        if len(rr_A) > 1 and beg >= 0 and end == len(resp.rr) - 1:
            random.shuffle(rr_A)
            resp.rr = rr_other + rr_A

    def __decrease_ttl_one(self):
        l = []
        for k, (v, _) in self.cache.iteritems():
            for rr in v.rr:
                if rr.ttl <= 1:
                    l.append(k)
                else:
                    rr.ttl -= 1
        for k in l:
            self.cache.remove(k)
