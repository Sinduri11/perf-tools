#!/usr/bin/env python
# A module for processing LBR streams
# Author: Ahmad Yasin
# edited: July 2022
#
from __future__ import print_function
__author__ = 'ayasin'
__version__= 0.84

import common as C
import pmu
import os, re, sys
from numpy import average

INDIRECT = r"(jmp|call).*%"

hitcounts = C.envfile('PTOOLS_HITS')
debug = os.getenv('DBG')
verbose = os.getenv('VER')
use_cands = os.getenv('LBR_USE_CANDS')

def hex(ip): return '0x%x' % ip if ip > 0 else '-'
def hist_fmt(d): return '%s%s' % (str(d).replace("'", ""), '' if 'num-buckets' in d and d['num-buckets'] == 1 else '\n')
def inc(d, b): d[b] = d.get(b, 0) + 1
def ratio(a, b): return '%.2f%%' % (100.0 * a / b)
def read_line(): return sys.stdin.readline()

def exit(x, sample, label):
  C.annotate(x, label)
  print_sample(sample, 0)
  C.printf(debug+'\n')
  sys.exit()

def str2int(ip, plist):
  try:
    return int(ip, 16)
  except ValueError:
    print_sample(plist[1])
    assert 0, "expect address in '%s' of '%s'" % (ip, plist[0])

def skip_sample(s):
  line = read_line()
  while not re.match(r"^$", line):
    line = read_line()
    assert line, 'was input truncated? sample:\n%s'%s
  return 0

def header_ip(line):
  x = is_header(line)
  assert x, "Not a head of sample: " + line
  return str2int(C.str2list(line)[6 if '[' in x.group(1) else 5], (line, None))

def line_ip(line, sample=None):
  x = re.match(r"\s+(\S+)\s+(\S+)", line)
  assert x, 'expect <address> at left of %s'%line
  ip = x.group(1).lstrip("0")
  return str2int(ip, (line, sample))

def line_timing(line):
  x = re.match(r"[^#]+# (\S+) (\d+) cycles \[\d+\] ([0-9\.]+) IPC", line)
  # note: this ignores timing of 1st LBR entry (has cycles but not IPC)
  assert x, 'Could not match IPC in:\n%s' % line
  ipc = round(float(x.group(3)), 1)
  cycles = int(x.group(2))
  return cycles, ipc

def tripcount(ip, loop_ipc, state):
  if state == 'new' and loop_ipc in loops:
    if not 'tripcount' in loops[loop_ipc]: loops[loop_ipc]['tripcount'] = {}
    state = 'valid'
  elif type(state) == int:
    if ip == loop_ipc: state += 1
    elif not is_in_loop(ip, loop_ipc):
      inc(loops[loop_ipc]['tripcount'], str(state))
      state = 'done'
  elif state == 'valid':
    if ip == loop_ipc:
      state = 1
  return state

def loop_stats(line, loop_ipc, tc_state):
  def mark(regex, tag):
    if re.findall(regex, line):
      if not loop_stats.atts or not tag in loop_stats.atts:
        loop_stats.atts = ';'.join((loop_stats.atts, tag)) if loop_stats.atts else tag
  def vec_reg(i): return '%%%smm' % chr(ord('x') + i)
  def vec_len(i): return 'vec%d' % (128 * (2**i))
  # loop-body stats, FIXME: on the 1st encoutered loop in a new sample for now
  # TODO: improve perf of loop_stats invocation
  #if (glob['loop_stats_en'] == 'No' or
  #  (glob['loop_stats_en'] == 'One' and line_ip(line) != loop_ipc and tc_state == 'new')):
  #  #not (is_loop(line) or (type(tcstate) == int)))):
  #  return tc_state
  #elif tc_state == 'new' and is_loop(line):
  if glob['loop_stats_en'] and tc_state == 'new' and is_loop(line):
    loop_stats.id = line_ip(line)
    loop_stats.atts = ''
  if loop_stats.id:
    if not is_in_loop(line_ip(line), loop_stats.id): # just exited a loop
      if len(loop_stats.atts) > len(loops[loop_stats.id]['attributes']):
        loops[loop_stats.id]['attributes'] = loop_stats.atts
        if debug and int(debug, 16) == loop_stats.id: print(loop_stats.atts, stat['total'])
      loop_stats.atts = ''
      loop_stats.id = None
    else:
      mark(INDIRECT, 'indirect')
      mark(r"[^k]s[sdh]([a-z])\s[\sa-z0-9,\(\)%%]+mm", 'scalar-fp')
      for i in range(loop_stats_vec):
        mark(r"[^k]p[sdh]\s+" + vec_reg(i), vec_len(i) + '-fp')
        mark(r"\s%sp.*%s" % ('(v)' if i==0 else 'v', vec_reg(i)), vec_len(i) + '-int')
  return tripcount(line_ip(line), loop_ipc, tc_state)
loop_stats.id = None
loop_stats.atts = ''
loop_stats_vec = 3 if pmu.cpu_has_feature('avx512vl') else 2

bwd_br_tgts = [] # better make it local to read_sample..
loop_cands = []
def detect_loop(ip, lines, loop_ipc,
  MOLD=4e4): #Max Outer Loop Distance
  global bwd_br_tgts, loop_cands # unlike nonlocal, global works in python2 too!
  def find_block_ip(x = len(lines)-2):
    while x>=0:
      if is_taken(lines[x]):
        return line_ip(lines[x+1]), x
      x -= 1
    return 0, -1
  def has_ip(at):
    while at > 0:
      if is_callret(lines[at]): return False
      if line_ip(lines[at]) == ip: return True
      at -= 1
    return False
  def iter_update():
    #inc(loop['BK'], hex(line_ip(lines[-1])))
    if ip != loop_ipc: return
    if not 'IPC' in loop: loop['IPC'] = {}
    if not has_timing(lines[-1]): return
    cycles = 0
    begin, at = find_block_ip()
    while begin:
      if begin == ip:
        if cycles == 0: inc(loop['IPC'], line_timing(lines[-1])[1]) # IPC is supported for loops execution w/ no takens
        if 'Conds' in loop and 'Cond_polarity' in loop:
          for c in loop['Cond_polarity'].keys(): loop['Cond_polarity'][c]['tk' if cycles else 'nt'] += 1
        cycles += line_timing(lines[-1])[0]
        glob['loop_cycles'] += cycles
        glob['loop_iters'] += 1
        break
      else:
        if has_timing(lines[at]):
          cycles += line_timing(lines[at])[0]
          begin, at = find_block_ip(at-1)
        else: break
  
  if ip in loops:
    loop = loops[ip]
    loop['hotness'] += 1
    if is_taken(lines[-1]): iter_update()
    if not loop['size'] and not loop['outer'] and len(lines)>2 and line_ip(lines[-1]) == loop['back']:
      size, cnt, conds = 1, {}, []
      types = ('taken', 'load', 'store', 'prefetch', 'lea', 'nop')
      for i in types: cnt[i] = 0
      x = len(lines)-2
      while x >= 1:
        size += 1
        if is_taken(lines[x]): cnt['taken'] += 1
        if re.match(r"\s+\S+\s+j[^m]", lines[x]): conds += [line_ip(lines[x])]
        if 'nop' in lines[x]: cnt['nop'] += 1
        elif '(' in lines[x]:
          if 'lea' in lines[x]: cnt['lea'] += 1
          elif 'prefetch' in lines[x]: cnt['prefetch'] += 1
          elif re.match(r"\s+\S+\s+(cmp[^x]|test)", lines[x]): cnt['load'] += 1
          else: cnt['store' if re.match(r"\s+\S+\s+[^\(\),]+,", lines[x]) else 'load'] += 1
        inst_ip = line_ip(lines[x])
        if inst_ip == ip:
          loop['size'], loop['Conds'] = size, len(conds)
          for i in types: loop[i] = cnt[i]
          if len(conds):
            loop['Cond_polarity'] = {}
            for c in conds: loop['Cond_polarity'][c] = {'tk': 0, 'nt': 0}
          if debug and int(debug, 16) == ip: print(size, stat['total'])
          break
        elif inst_ip < ip or inst_ip > loop['back']:
          break
        x -= 1
    if not loop['entry-block'] and not is_taken(lines[-1]):
      loop['entry-block'] = find_block_ip()[0]
    return
  
  # only simple loops, of these attributes, are supported:
  # * are entirely observed in a single sample (e.g. tripcount < 32)
  # * a tripcount > 1 is observed
  # * no function calls
  if is_taken(lines[-1]):
    xip = line_ip(lines[-1])
    if xip <= ip: pass # not a backward jump
    elif (use_cands and ip in loop_cands) or (not use_cands and ip in bwd_br_tgts):
      if use_cands: loop_cands.remove(ip)
      else: bwd_br_tgts.remove(ip)
      inner, outer = 0, 0
      ins, outs = set(), set()
      for l in loops:
        if ip > l and xip < loops[l]['back']:
          inner += 1
          outs.add(hex(l))
          loops[l]['outer'] = 1
          loops[l]['inner-loops'].add(hex(ip))
        if ip < l and xip > loops[l]['back']:
          outer = 1
          ins.add(hex(l))
          loops[l]['inner'] += 1
          loops[l]['outer-loops'].add(hex(ip))
      loops[ip] = {'back': xip, 'hotness': 1, 'size': None, 'attributes': '',
        'entry-block': 0 if xip > ip else find_block_ip()[0], #'BK': {hex(xip): 1, },
        'inner': inner, 'outer': outer, 'inner-loops': ins, 'outer-loops': outs
      }
      return
    elif use_cands and len(lines) > 2 and ip in bwd_br_tgts and has_ip(len(lines)-2):
      bwd_br_tgts.remove(ip)
      loop_cands += [ip]
    elif (((xip - ip) < MOLD) and
        not is_callret(lines[-1]) and # requires --xed
        not ip in bwd_br_tgts and
        (use_cands or has_ip(len(lines)-2))):
      bwd_br_tgts += [ip]

LBR_Event = pmu.lbr_event()[:-4]
lbr_events = []
loops = {}
stat = {x: 0 for x in ('bad', 'bogus', 'total', 'total_cycles')}
stat['IPs'] = {}
stat['events'] = {}
stat['size'] = {'min': 0, 'max': 0, 'avg': 0}
size_sum=0
glob = {x: 0 for x in ('loop_cycles', 'loop_iters')}
hsts = {}
footprint = set()
pages = set()
indirects = set()

def read_sample(ip_filter=None, skip_bad=True, min_lines=0, labels=False,
                loop_ipc=0, lp_stats_en=False, event = LBR_Event, indirect_en=True):
  global lbr_events, size_sum, bwd_br_tgts
  valid, lines, bwd_br_tgts = 0, [], []
  glob['size_stats_en'] = skip_bad and not labels
  glob['loop_stats_en'] = lp_stats_en
  glob['ip_filter'] = ip_filter
  edge_en = event.startswith(LBR_Event) and not ip_filter and not loop_ipc # config good for edge-profile
  if stat['total'] == 0 and edge_en:
    if indirect_en:
      for x in ('', '-misp'): hsts['indirect-x2g%s' % x] = {}
    if os.getenv('LBR_INDIRECTS'):
      for x in os.getenv('LBR_INDIRECTS').split(','):
        indirects.add(int(x, 16))
        hsts['indirect_%s_targets' % x] = {}
        hsts['indirect_%s_paths' % x] = {}
    if pmu.dsb_msb() and not pmu.cpu('smt-on'): hsts['dsb-heatmap'] = {}
  if stat['total']==0 and debug: C.printf('DBG=%s\n' % debug)
  tick = int(os.getenv('LBR_TICK')) if os.getenv('LBR_TICK') else 1000
  if loop_ipc: tick *= 10
  
  while not valid:
    valid, lines, bwd_br_tgts = 1, [], []
    xip, timestamp = None, None
    tc_state = 'new'
    stat['total'] += 1
    if stat['total'] % tick == 0: C.printf('.')
    while True:
      line = read_line()
      # input ended
      if not line:
        if len(lines): stat['bogus'] += 1
        if stat['total'] == stat['bogus']:
          print_all()
          C.error('No LBR data in profile')
        if not loop_ipc: C.printf(' .\n')
        return lines if len(lines) and not skip_bad else None
      header = is_header(line)
      if header:
        # first sample here (of a given event)
        ev = header.group(3)[:-1]
        if not ev in lbr_events:
          lbr_events += [ev]
          x = 'events= %s @ %s' % (str(lbr_events), header.group(1).split(' ')[-1])
          if len(lbr_events) == 1: x += ' primary= %s %s' % (event, C.env2str('LBR_STOP'))
          if ip_filter: x += ' ip_filter= %s' % ip_filter
          if loop_ipc: x += ' loop= %s' % hex(loop_ipc)
          C.printf(x+'\n')
        inc(stat['events'], ev)
        if debug: timestamp = header.group(1).split()[-1]
      # a new sample started
      # perf  3433 1515065.348598:    1000003 EVENT.NAME:      7fd272e3b217 __regcomp+0x57 (/lib/x86_64-linux-gnu/libc-2.23.so)
        if ip_filter:
          if not ip_filter in line:
            valid = skip_sample(line)
            break
          inc(stat['IPs'], ip_filter)
      # a sample ended
      if re.match(r"^$", line):
        len_m1 = 0
        if len(lines): len_m1 = len(lines)-1
        if len_m1 == 0 or\
           min_lines and (len_m1 < min_lines) or\
           header_ip(lines[0]) != line_ip(lines[len_m1]):
          valid = 0
          stat['bogus'] += 1
          if debug and debug == timestamp:
            exit((line.strip(), len(lines)), lines, 'a bogus sample ended')
        elif len_m1 and type(tc_state) == int and is_in_loop(line_ip(lines[-1]), loop_ipc):
          if tc_state == 31 or verbose:
            inc(loops[loop_ipc]['tripcount'], '%d+' % (tc_state + 1))
          # else: note a truncated tripcount, i.e. unknown in 1..31, is not accounted for by default.
        if debug and debug == timestamp:
          exit((line.strip(), len(lines)), lines, 'sample-of-interest ended')
        break
      elif header and len(lines): # sample had no LBR data; new one started
        # exchange2_r_0.j 57729 3736595.069891:    1000003 r20c4:pp:            41f47a brute_force_mp_brute_+0x43aa (/home/admin1/ayasin/perf-tools/exchange2_r_0.jmpi4)
        # exchange2_r_0.j 57729 3736595.069892:    1000003 r20c4:pp:            41fad4 brute_force_mp_brute_+0x4a04 (/home/admin1/ayasin/perf-tools/exchange2_r_0.jmpi4)
        lines = []
        stat['bogus'] += 1 # for this one
        stat['total'] += 1 # for new one
      # invalid sample is about to end
      if skip_bad and 'not reaching sample' in line:
        valid = 0
        stat['bad'] += 1
        assert re.match(r"^$", read_line())
        break
      # a line with a label
      if not labels and is_label(line):
        continue
      # e.g. "        00007ffff7afc6ca        <bad>" then "mismatch of LBR data and executable"
      if 'mismatch of LBR data' in line:
        valid = skip_sample(lines[0])
        stat['bad'] += 1
        break
      # e.g. "        prev_nonnote_           addb  %al, (%rax)"
      if skip_bad and len(lines) and not line.strip().startswith('0'):
        if debug and debug == timestamp:
          exit(line, lines, "bad line")
        valid = skip_sample(lines[0])
        stat['bogus'] += 1
        break
      ip = None if header or is_label(line) else line_ip(line, lines)
      new_line = is_line_start(ip, xip)
      if edge_en and new_line:
        footprint.add(ip >> 6)
        pages.add(ip >> 12)
      if len(lines) and not is_label(line):
        # a 2nd instruction
        if len(lines) > 1:
          detect_loop(ip, lines, loop_ipc)
          if 'dsb-heatmap' in hsts and (is_taken(lines[-1]) or new_line):
            inc(hsts['dsb-heatmap'], pmu.dsb_set_index(ip))
          # TODO: consider the branch instruction's bytes (once support added to perf-script)
          if 'indirect-x2g' in hsts and re.findall(INDIRECT, lines[-1]) and abs(ip - xip) >= 2**31:
            inc(hsts['indirect-x2g'], xip)
            if 'MISP' in lines[-1]: inc(hsts['indirect-x2g-misp'], xip)
          if xip in indirects:
            inc(hsts['indirect_%s_targets' % hex(xip)], ip)
            inc(hsts['indirect_%s_paths' % hex(xip)], '%s.%s.%s' % (hex(get_taken(lines, -2)['from']), hex(xip), hex(ip)))
        tc_state = loop_stats(line, loop_ipc, tc_state)
      if len(lines) or event in line:
        line = line.rstrip('\r\n')
        if has_timing(line):
          cycles = line_timing(line)[0]
          stat['total_cycles'] += cycles
        lines += [ line ]
      xip = ip
  if glob['size_stats_en']:
    size = len(lines) - 1
    if size_sum == 0: stat['size']['min'] = stat['size']['max'] = size
    else:
      if stat['size']['min'] > size: stat['size']['min'] = size
      if stat['size']['max'] < size: stat['size']['max'] = size
    size_sum += size
  return lines

def is_callret(l):    return re.findall(r"(call|ret)", l)

def is_header(line): return re.match(r"([^:]*):\s+(\d+)\s+(\S*)\s+(\S*)", line)

def is_jmp_next(br, # a hacky implementation for now
  JS=2,             # short direct Jump Size
  CDLA=16):         # compiler default loops alignment
  mask = ~(CDLA - 1)
  return (br['to'] == (br['from'] + JS)) or (
         (br['to'] & mask) ==  ((br['from'] & mask) + CDLA))

def has_timing(line): return line.endswith('IPC')
def is_line_start(ip, xip): return (ip >> 6) ^ (xip >> 6) if ip and xip else False
def is_label(line):   return line.strip().endswith(':')
def is_loop(line):    return line_ip(line) in loops
def is_taken(line):   return '# ' in line
def is_in_loop(ip, loop): return ip >= loop and ip <= loops[loop]['back']
def get_inst(l):      return C.str2list(l)[1]
def get_loop(ip):     return loops[ip] if ip in loops else None

def get_taken(sample, n):
  assert n in range(-32, 0), 'invalid n='+str(n)
  i = len(sample)-1
  frm, to = -1, -1
  while i >= 0:
    if is_taken(sample[i]):
      n += 1
      if n==0:
        frm = line_ip(sample[i], sample)
        if i < (len(sample)-1): to = line_ip(sample[i+1], sample)
        break
    i -= 1
  return {'from': frm, 'to': to, 'taken': 1}

def print_loop_hist(loop_ipc, name, weighted=False, sortfunc=None):
  loop = loops[loop_ipc]
  assert name in loop
  d = print_hist((loop[name], name, loop, loop_ipc, sortfunc, weighted))
  if not type(d) is dict: return d
  tot = d['total']
  del d['total']
  del d['type']
  for x in d.keys(): loop['%s-%s' % (name, x)] = d[x]
  print('')
  return tot

def print_glob_hist(hist, name):
  d = print_hist((hist, name))
  if not type(d) is dict: return d
  if d['type'] == 'hex':
    d['mode'] = hex(int(d['mode']))
  del d['type']
  print('%s histogram summary: %s' % (name, hist_fmt(d)))

def print_hist(hist_t, Threshold=0.01):
  if not hist_t[0]: return -1
  hist, name = hist_t[0], hist_t[1]
  loop, loop_ipc, sorter, weighted = (None, ) * 4
  if len(hist_t) > 2: (loop, loop_ipc, sorter, weighted) = hist_t[2:]
  tot = sum(hist.values())
  if debug: C.printf('%s tot=%d\n' % (name, tot))
  if not tot: return 0
  d = {}
  d['type'] = 'paths' if 'paths' in name else ('hex' if 'indir' in name else 'number')
  shist = sorted(hist.items(), key=lambda x: x[1])
  d['mode'] = str(shist[-1][0])
  keys = [sorter(x) for x in hist.keys()] if sorter else list(hist.keys())
  if d['type'] == 'number': d['mean'] = str(round(average(keys, weights=list(hist.values())), 2))
  d['num-buckets'] = len(hist)
  if d['num-buckets'] > 1:
    C.printc('%s histogram%s:' % (name, ' of loop %s' % hex(loop_ipc) if loop_ipc else ''))
    left, threshold = 0, int(Threshold * tot)
    for k in sorted(hist.keys(), key=sorter):
      if hist[k] >= threshold:
        print('%5s: %6d%6.1f%%' % (hex(k) if d['type'] == 'hex' else k, hist[k], 100.0 * hist[k] / tot))
      else: left += hist[k]
    if left: print('other: %6d%6.1f%%\t// buckets < %.1f%%' % (left, 100.0 * left / tot, 100.0 * Threshold))
  d['total'] = sum(hist[k] * int(k.split('+')[0]) for k in hist.keys()) if weighted else tot
  return d

def print_hist_sum(name, h):
  print('count of %s: %d' % (name, sum(hsts[h].values())))

def print_all(nloops=10, loop_ipc=0):
  stat['detected-loops'] = len(loops)
  total = stat['IPs'][glob['ip_filter']] if glob['ip_filter'] else stat['total']
  if glob['size_stats_en']: stat['size']['avg'] = round(size_sum / (total - stat['bad'] - stat['bogus']), 1)
  if not loop_ipc: print('LBR samples:', hist_fmt(stat))
  if total and (stat['bad'] + stat['bogus']) / float(total) > 0.5: C.error('Too many LBR bad/bogus samples in profile')
  if os.getenv('PTOOLS_CYCLES'): print('LBR cycles coverage (scaled by 1K): %s' % ratio(1e3 * stat['total_cycles'], int(os.getenv('PTOOLS_CYCLES'))))
  if len(footprint): print('hot code footprint estimate: %.2f KB' % (len(footprint) / 16.0))
  if len(pages): print('estimate number of hot code 4K-pages: %d' % len(pages))
  if 'indirect-x2g' in hsts:
    print_hist_sum('indirect call/jump of >2GB offset', 'indirect-x2g')
    print_hist_sum('mispredicted indirect call/jump of >2GB offset', 'indirect-x2g-misp')
    for x in indirects:
      if x in hsts['indirect-x2g-misp'] and x in hsts['indirect-x2g']:
        print('misprediction ratio for indirect branch at %s: %s' % (hex(x), ratio(hsts['indirect-x2g-misp'][x], hsts['indirect-x2g'][x])))
  for x in sorted(hsts.keys()): print_glob_hist(hsts[x], x)
  sloops = sorted(loops.items(), key=lambda x: loops[x[0]]['hotness'])
  if loop_ipc:
    if loop_ipc in loops:
      lp = loops[loop_ipc]
      tot = print_loop_hist(loop_ipc, 'IPC')
      if glob['loop_iters']: lp['cyc/iter'] = '%.2f' % (glob['loop_cycles'] / glob['loop_iters'])
      lp['FL-cycles%'] = ratio(glob['loop_cycles'], stat['total_cycles'])
      if 'Cond_polarity' in lp and len(lp['Cond_polarity']) == 1 and lp['taken'] < 2:
        for c in lp['Cond_polarity'].keys():
          lp['%s_taken' % hex(c)] = ratio(lp['Cond_polarity'][c]['tk'], lp['Cond_polarity'][c]['tk'] + lp['Cond_polarity'][c]['nt'])
      tot = print_loop_hist(loop_ipc, 'tripcount', True, lambda x: int(x.split('+')[0]))
      if tot: lp['tripcount-coverage'] = ratio(tot, lp['hotness'])
      if hitcounts and lp['size'] and lp['taken'] == 0:
        C.exe_cmd('%s && echo' % C.grep('0%x' % loop_ipc, hitcounts, '-B1 -A%d' % lp['size']),
          'Hitcounts & ASM of loop %s' % hex(loop_ipc))
      find_print_loop(loop_ipc, sloops)
    else:
      C.warn('Loop %s was not observed'%hex(loop_ipc))
  if nloops and len(loops):
    if os.getenv("LBR_LOOPS_LOG"):
      log = open(os.getenv("LBR_LOOPS_LOG"), 'w')
      num = len(loops)
      for l in sloops:
        print_loop(l[0], num, log)
        num -= 1
      log.close()
    ploops = sloops
    if len(loops) > nloops: ploops = sloops[-nloops:]
    else: nloops = len(ploops)
    C.printc('top %d loops:' % nloops)
    for l in ploops:
      print_loop(l[0], nloops)
      nloops -=  1

def print_br(br):
  print('[from: %s, to: %s, taken: %d]' % (hex(br['from']), hex(br['to']), br['taken']))

def find_print_loop(ip, sloops):
  num = 1
  for l in reversed(sloops):
    if l[0] == ip:
      print_loop(l[0], num, detailed=True)
      print('\n'*2)
      return
    num += 1

def print_loop(ip, num=0, print_to=sys.stdout, detailed=False):
  if not isinstance(ip, int): ip = int(ip, 16) #should use (int, long) but fails on python3
  def printl(s, end=''): return print(s, file=print_to, end=end)
  if not ip in loops:
    printl('No loop was detected at %s!' % hex(ip), '\n')
    return
  loop = loops[ip].copy()
  def set2str(s, top=0 if detailed else 3):
    new = loop[s]
    if top and len(new) > top:
      n = len(new) - top
      new = set()
      while top > 0:
        new.add(loop[s].pop())
        top -= 1
      new.add('.. %d more'%n)
    loop[s] = C.chop(str(sorted(new, reverse=True)), (")", 'set('))
  fixl = ('hotness', 'FL-cycles%', 'size') if glob['loop_cycles'] else ('hotness', 'size')
  loop['hotness'] = '%6d' % loop['hotness']
  loop['size'] = str(loop['size']) if loop['size'] else '-'
  printl('%soop#%d: [ip: %s, ' % ('L' if detailed else 'l', num, hex(ip)))
  for x in fixl: printl('%s: %s, ' % (x, loop[x]))
  if not glob['loop_stats_en']: del loop['attributes']
  elif not len(loop['attributes']): loop['attributes'] = '-'
  elif ';' in loop['attributes']: loop['attributes'] = ';'.join(sorted(loop['attributes'].split(';')))
  dell = ['hotness', 'FL-cycles%', 'size', 'back', 'entry-block', 'IPC', 'tripcount']
  if 'taken' in loop and loop['taken'] <= loop['Conds']: dell += ['taken']
  if not verbose: dell += ['Cond_polarity', 'cyc/iter'] # No support for >1 Cond. cyc/iter needs debug (e.g. 548-xm3-basln)
  for x in ('back', 'entry-block'): printl('%s: %s, ' % (x, hex(loop[x])))
  for x, y in (('inn', 'out'), ('out', 'inn')):
    if loop[x + 'er'] > 0: set2str(y + 'er-loops')
    else: dell += [y + 'er-loops']
  for x in dell:
    if x in loop: del loop[x]
  printl(C.chop(str(loop), "'{}\"") + ']', '\n')

def print_sample(sample, n=10):
  if not len(sample): return
  C.printf('\n'.join(('sample#%d size=%d' % (stat['total'], len(sample)-1), sample[0], '\n')))
  C.printf('\n'.join((sample[-min(n, len(sample)-1):] if n else sample) + ['\n']))
  sys.stderr.flush()

def print_header():
  C.printc('Global stats:')
  print("perf-tools' lbr.py module version %.2f" % __version__)
