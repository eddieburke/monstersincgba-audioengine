#!/usr/bin/env python3
"""hachiemon_xm_exporter.py -- XM exporter for the Hachiemon (Natsume GBA) sound
driver, reverse-engineered from Monsters, Inc. (GBA, UE).

Subcommands
-----------
  info      [rom]                     tables + song list + driver facts
  dump      [rom] [-o out.json]       parse every song to JSON
  samples   [rom] [outdir]            extract all sample blobs (.s8 + .wav + manifest)
  xm        [rom] [outdir=EXPORTS] [songs..]  export XM modules
  render-xm <xm> <out.wav> [secs]     render an exported XM with the built-in player
  render    [rom] <song> <out.wav>    render a song from the parsed data (approximate)
  capture   [rom] [outdir] [songs..]  hardware-accurate capture via mGBA (patched ROMs)
  cut       [rom] [capdir] [outdir]   cut captures into one-pass WAV renders + manifest
  validate  [rom] [rendersdir]        check parsed drum hits against the rendered audio

Everything is stdlib-only except numpy, which is needed for render/playback
(`render-xm`, `render`, `cut`, `validate`); the parsing/export/capture paths
work without it.
"""
import json, math, os, shutil, signal, struct, subprocess, sys, time, wave
from bisect import bisect_right

try:
    import numpy as np
except ImportError:                      # parsing/exporting still work
    np = None


# ==========================================================================
# CORE (parser)
# ==========================================================================
"""Hachiemon (Natsume GBA sound driver) sequence parser & dumper.

Verified live against Monsters, Inc. (UE) in mGBA (RAM dump + breakpoints),
and against the driver disassembly in this ROM.
"""
BASE = 0x08000000

# channel slot -> engine type (fixed per slot, see 0x0803853C/init and live RAM)
ENGINE = [0,1,2,3, 0,1,2,3, 4,5,4,5]

OP_SIZE = {
 0xE0:1, 0xE1:1, 0xE2:1, 0xE3:1, 0xE4:0, 0xE5:0, 0xE6:0, 0xE7:0,
 0xE8:1, 0xE9:1, 0xEA:1, 0xEB:0, 0xEC:0, 0xED:0, 0xEE:0, 0xEF:0,
 0xF0:1, 0xF1:0, 0xF2:1, 0xF3:0, 0xF4:4, 0xF5:4, 0xF6:0, 0xF7:1,
 0xF8:1, 0xF9:2, 0xFA:1, 0xFB:4, 0xFC:0, 0xFD:0, 0xFE:1, 0xFF:0,
}
CMD_NAME = {
 0xE0:'REST_L', 0xE1:'DUR_BASE', 0xE2:'PARAM_5D', 0xE3:'INSTRUMENT',
 0xE4:'DUTY_12', 0xE5:'DUTY_25', 0xE6:'DUTY_50', 0xE7:'DUTY_75',
 0xE8:'TRANSPOSE', 0xE9:'ECHO', 0xEA:'PARAM_63',
 0xEB:'(quirk: note)', 0xEC:'(quirk: note)', 0xED:'(quirk: note)',
 0xEE:'(quirk: note)', 0xEF:'(quirk: note)',
 0xF0:'LOOP1', 0xF1:'LOOP1_END', 0xF2:'LOOP2', 0xF3:'LOOP2_END',
 0xF4:'JUMP', 0xF5:'CALL', 0xF6:'RETURN', 0xF7:'VELOCITY', 0xF8:'PARAM_49',
 0xF9:'PITCH_SLIDE', 0xFA:'DUR_HIGH', 0xFB:'MODULATION', 0xFC:'MOD_ON',
 0xFD:'MOD_OFF', 0xFE:'PLAY_MODE', 0xFF:'END',
}

class Rom:
    def __init__(self, path):
        self.data = open(path, 'rb').read(); self.N = len(self.data)
    def u8(self, a):  return self.data[a-BASE]
    def u16(self, a): return struct.unpack_from('<H', self.data, a-BASE)[0]
    def u32(self, a): return struct.unpack_from('<I', self.data, a-BASE)[0]
    def blob(self, a, n): return self.data[a-BASE:a-BASE+n]
    def ok(self, a, n=1): return BASE <= a and a+n <= BASE+self.N

class Hachiemon:
    # ---- Monsters, Inc. (UE) addresses (per-build; found via the driver's
    #      table set loaded at 0x08000288.. and the RAM dump) ----
    SONG_TABLE   = 0x080691B0   # u32[20]
    INSTR_TABLE  = 0x0806948C   # 28 x 8-byte records
    SAMPLE_TAB1  = 0x0806A1E0
    SAMPLE_TAB2  = 0x0806A2E0
    DRUM_TABLE   = 0x0806A338   # u16 count, u16 base_note, u32 ptr[count]
    NOTE_TABLE   = 0x08132108   # 72 x u16 GB frequency (11-bit) values
    WAVE_TABLE   = 0x0806944C   # 4 x 16 bytes (wave RAM images)
    NOTE_BASE    = 0x24         # index = clamp(note_byte + transpose - 0x24, 0, 0x47)
    VBLANK_HZ    = 59.7275

    def __init__(self, path):
        self.path = os.fspath(path)
        self.rom = Rom(path)

    # ---------- tables ----------
    def note_to_reg(self, note, transpose=0):
        i = max(0, min(0x47, note + transpose - self.NOTE_BASE))
        return self.rom.u16(self.NOTE_TABLE + 2*i)

    @staticmethod
    def reg_to_hz(x):
        x &= 0x7FF
        return 131072.0 / (2048.0 - x)

    def note_to_hz(self, note, transpose=0):
        return self.reg_to_hz(self.note_to_reg(note, transpose))

    def songs(self):
        out = []
        for i in range(20):
            p = self.rom.u32(self.SONG_TABLE + 4*i)
            out.append((i, p))
        return out

    def parse_header(self, addr):
        r = self.rom
        mask = r.u16(addr)
        b2 = r.u8(addr+2); b3 = r.u8(addr+3)
        n = bin(mask).count('1')
        ptrs = [r.u32(addr+4+4*i) for i in range(n)]
        # mask is walked MSB-first (bit11 -> channel 0, bit0 -> channel 11);
        # stream pointers are assigned to ascending channel slots.
        slots = []
        for bit in range(11, -1, -1):
            if mask & (1 << bit):
                slots.append(11-bit)
        return dict(addr=addr, mask=mask, unk2=b2, unk3=b3,
                    channels=ptrs, slots=slots)

    # ---------- stream parsing ----------
    def parse_stream(self, ptr, engine, dur_base=0, max_steps=200000,
                     follow_loops=False):
        """Return (events, end_tick, info).

        events: list of (tick, kind, a, b, c)
          ('note', note_byte, index, ticks, freq_hz)   index=None for engines 4-6
          ('rest', ticks_dummy, None, ticks, None)
          ('vol', n, ...), ('duty', ...), ('instr', ...), ('mode', ...),
          ('loop', 'JUMP'|'FF', target, tick, None)
        Stops after one pass: at the first backward JUMP unless follow_loops.
        """
        r = self.rom
        pc = ptr; start = ptr
        tick = 0; mode = 0; transpose = 0; dur_hi = 0; dbase = dur_base
        loop1 = None; loop2 = None; ret = None
        events = []; steps = 0
        loop_target = None
        while steps < max_steps:
            steps += 1
            if not r.ok(pc, 1):
                events.append((tick, 'err', pc, 0, None)); break
            b = r.u8(pc); pc += 1
            if b < 0x80:                            # note
                if mode == 1:
                    ticks = max(1, dbase)
                    if engine in (0,1,2,4,5) and r.ok(pc,1):
                        extra = r.u8(pc); pc += 1
                    else:
                        extra = None
                    idx = None; hz = None
                    if engine in (0,1,2,3):
                        idx = max(0, min(0x47, b + transpose - self.NOTE_BASE))
                        hz = self.reg_to_hz(self.rom.u16(self.NOTE_TABLE+2*idx))
                    events.append((tick, 'note', b, idx, ticks, hz, extra))
                else:
                    d = r.u8(pc); pc += 1
                    ticks = ((dur_hi << 8) | d) + 1; dur_hi = 0
                    if mode == 2 and engine in (3,4,5):
                        # drum / sample trigger: index = clamp(note-base,0,count-1)
                        cnt = r.u16(self.DRUM_TABLE); base_n = r.u16(self.DRUM_TABLE+2)
                        idx = max(0, min(cnt-1, b - base_n))
                        events.append((tick, 'drum', b, idx, ticks, None, None))
                    elif engine in (0,1,2,3):
                        idx = max(0, min(0x47, b + transpose - self.NOTE_BASE))
                        hz = self.reg_to_hz(self.rom.u16(self.NOTE_TABLE+2*idx))
                        events.append((tick, 'note', b, idx, ticks, hz, None))
                    else:
                        events.append((tick, 'pcm', b, b, ticks, None, None))
                tick += ticks
            elif b < 0x90:                          # 0x80-0x8F rest
                d = r.u8(pc); pc += 1
                ticks = d + 1; dur_hi = 0
                events.append((tick, 'rest', None, None, ticks, None, None))
                tick += ticks
            elif b < 0xD0:                          # 0x90-0xCF ignored (no operand)
                events.append((tick, 'nop', b, None, 0, None, None))
            elif b < 0xE0:                          # 0xD0-0xDF volume (nibble in opcode)
                events.append((tick, 'vol', b & 0xF, None, 0, None, None))
            else:
                sz = OP_SIZE.get(b, 0)
                args = [r.u8(pc+i) for i in range(sz)]; pc += sz
                if b == 0xE0:                       # long rest (same handler as 0x80)
                    ticks = (args[0] & 0xFF) + 1
                    events.append((tick, 'rest', None, None, ticks, None, None)); tick += ticks
                elif b == 0xE1: dbase = args[0]
                elif b == 0xE8:
                    transpose = args[0]-256 if args[0] >= 128 else args[0]
                    events.append((tick,'transpose',transpose,None,0,None,None))
                elif b == 0xE3: events.append((tick, 'instr', args[0], None, 0, None, None))
                elif b in (0xE4,0xE5,0xE6,0xE7):
                    events.append((tick, 'duty', b-0xE4, None, 0, None, None))
                elif b == 0xF0: loop1 = (pc, args[0])
                elif b == 0xF1:
                    if loop1 and loop1[1] > 0:
                        pos, cnt = loop1
                        if cnt > 1: loop1 = (pos, cnt-1); pc = pos
                        else: loop1 = None
                elif b == 0xF2: loop2 = (pc, args[0])
                elif b == 0xF3:
                    if loop2 and loop2[1] > 0:
                        pos, cnt = loop2
                        if cnt > 1: loop2 = (pos, cnt-1); pc = pos
                        else: loop2 = None
                elif b == 0xF4:
                    target = args[0] | args[1]<<8 | args[2]<<16 | args[3]<<24
                    events.append((tick, 'jump', target, None, 0, None, None))
                    if target <= pc:
                        if not follow_loops:
                            events.append((tick, 'loopend', target, None, 0, None, None))
                            break
                    pc = target
                elif b == 0xF5:
                    target = args[0] | args[1]<<8 | args[2]<<16 | args[3]<<24
                    ret = pc; pc = target
                    events.append((tick, 'call', target, None, 0, None, None))
                elif b == 0xF6:
                    if ret: pc, ret = ret, None
                    else: break
                elif b == 0xF7: events.append((tick, 'velocity', args[0], None, 0, None, None))
                elif b == 0xF8: events.append((tick, 'param49', args[0], None, 0, None, None))
                elif b == 0xF9: events.append((tick, 'slide', args[0], args[1], 0, None, None))
                elif b == 0xFA: dur_hi = args[0]
                elif b == 0xFB: events.append((tick, 'mod', args[0], args[1], args[2], args[3], None))
                elif b == 0xFC: events.append((tick, 'mod_on', None, None, 0, None, None))
                elif b == 0xFD: events.append((tick, 'mod_off', None, None, 0, None, None))
                elif b == 0xFE: mode = args[0]
                elif b == 0xFF:
                    events.append((tick, 'end', None, None, 0, None, None)); break
                elif b in (0xEB,0xEC,0xED,0xEE,0xEF):
                    # these fall through to the note handler in the driver
                    idx = max(0, min(0x47, b + transpose - self.NOTE_BASE))
                    hz = self.reg_to_hz(self.rom.u16(self.NOTE_TABLE+2*idx))
                    d = r.u8(pc); pc += 1; ticks = ((dur_hi<<8)|d)+1; dur_hi = 0
                    events.append((tick, 'note', b, idx, ticks, hz, None)); tick += ticks
        return events, tick

    def song(self, idx):
        songs = self.songs()
        _, hdr_addr = songs[idx]
        h = self.parse_header(hdr_addr)
        chans = []
        for slot, ptr in zip(h['slots'], h['channels']):
            eng = ENGINE[slot]
            ev, end = self.parse_stream(ptr, eng)
            chans.append(dict(slot=slot, engine=eng, stream=ptr, events=ev, ticks=end))
        ticks = max(c['ticks'] for c in chans) if chans else 0
        return dict(index=idx, header=hdr_addr, mask=h['mask'], unk2=h['unk2'],
                    channels=chans, ticks=ticks, seconds=ticks/self.VBLANK_HZ)

# ==========================================================================
# SAMPLE EXTRACTION
# ==========================================================================
# ============================================================================
# 2. SAMPLE EXTRACTION
# ============================================================================
TAB1 = 0x0806A1E0        # 64 melodic PCM blobs
TAB2 = 0x0806A2E0        # 16 drum blobs

def read_blob(h, p):
    """Sample blob: u32 size @+0, u16 rate @+4, u8 scale @+6, u8 base note @+7,
    u32 loop @+8, u32 loop start @+0xC, signed 8-bit PCM @+0x10."""
    r = h.rom
    size, rate, scale, base = r.u32(p), r.u16(p + 4), r.u8(p + 6), r.u8(p + 7)
    audio = r.blob(p + 0x10, size)
    return dict(addr=p, size=size, rate=rate, scale=scale, base_note=base,
                loop=r.u32(p + 8), loop_start=r.u32(p + 0xC), audio=bytes(audio))

def write_wav8(path, audio, rate):
    with wave.open(path, 'wb') as w:      # WAV 8-bit is unsigned, GBA data signed
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(rate)
        w.writeframes(bytes((b + 128) & 0xFF for b in audio))

def extract_samples(h, outdir='samples', quiet=False):
    raw_d, wav_d = os.path.join(outdir, 'raw'), os.path.join(outdir, 'wav')
    os.makedirs(raw_d, exist_ok=True); os.makedirs(wav_d, exist_ok=True)
    manifest, seen = [], {}
    for tag, tab, count in (('t1', TAB1, 64), ('t2', TAB2, 16)):
        for i in range(count):
            p = h.rom.u32(tab + 4 * i)
            b = read_blob(h, p)
            name = '%s_%02d' % (tag, i)
            if p not in seen:
                open(os.path.join(raw_d, name + '.s8'), 'wb').write(b['audio'])
                write_wav8(os.path.join(wav_d, name + '.wav'), b['audio'], b['rate'])
                seen[p] = name
            manifest.append(dict(table=tag, index=i, name=name, addr='0x%08X' % p,
                                 size=b['size'], rate_hz=b['rate'], scale=b['scale'],
                                 base_note=b['base_note'], loop=b['loop'],
                                 loop_start=b['loop_start'],
                                 seconds=round(b['size'] / b['rate'], 4),
                                 raw=os.path.join(raw_d, seen[p] + '.s8'),
                                 wav=os.path.join(wav_d, seen[p] + '.wav'),
                                 duplicate_of=None if seen[p] == name else seen[p]))
    json.dump(manifest, open(os.path.join(outdir, 'manifest.json'), 'w'), indent=1)
    if not quiet:
        uniq = len(set(m['addr'] for m in manifest))
        print('%d unique sample blobs (%d table entries), %d bytes' %
              (uniq, len(manifest), sum(m['size'] for m in manifest if not m['duplicate_of'])))
    return manifest

# ==========================================================================
# XM EXPORT
# ==========================================================================
"""XM export from per-frame driver state and hardware register semantics.

ADSR, F9 modulation, E9 history, NR43 percussion and PCM timer quantization
are evaluated before compiling the tracker rows. Pitch changes use tick-zero
E1/E2 and X1/X2 effects. Tempo and loop control have separate channels.

PSG edges and PCM sample holds are expanded to reduce interpolation loss.
The export preserves editable notes and effects, but XM does not emulate the
combined GBA DAC, oscillator phase, or every PWM alias product. See audit/.
"""

GBA_TICK_HZ = 59.7275
SEQ_TAB1 = 0x0806A1E0          # PCM bank 1 (64 melodic sample descriptors)
SEQ_TAB2 = 0x0806A2E0          # PCM bank 2 (16 drum sample descriptors)
DRUM_TAB = 0x0806A338      # drum macro bank: u16 count, u16 first note, ptrs
FNUM_TAB = 0x08132108      # 72-entry note -> period table
WAVE_LUT = 0x0813219A      # 16-entry wave-channel level table
Q12_TAB_ADDR = 0x081321F6  # 12 u16 semitone multipliers (Q12), used by 080391BC
PCM_RATE_MAX = 0x78000     # 0803926C rate clamp (0xF0 << 11)

# Verified at export time against ROM bytes at Q12_TAB_ADDR.
Q12_TAB = [4096, 4339, 4597, 4870, 5160, 5467, 5792, 6137,
           6501, 6888, 7298, 7732]

def q12_mult(s):
    """080391BC: Q12 fixed-point 2^(s/12), bit-exact.

    s > 0: table[s % 12] << (s // 12); s < 0: with n = -s,
    table[(12 - n % 12) % 12] >> (n // 12 + (1 if n % 12 else 0)).
    Truncation of the table entries and shifts is included verbatim."""
    if s > 0:
        return Q12_TAB[s % 12] << (s // 12)
    if s < 0:
        n = -s
        q, m = n // 12, n % 12
        if m:
            return Q12_TAB[12 - m] >> (q + 1)
        return Q12_TAB[0] >> q
    return 4096

def pcm_rate(native, semis):
    """08039290 + 0803926C: DirectSound DMA rate in Hz (integer, bit-exact).

    rate = (native * Q12(pitch - base)) >> 12, clamped to 0x78000.
    There is NO low-side or 6-octave clamp: negative (pitch - base)
    plays below the native rate. The fine-scale byte (blob+6) is not
    applied: its code path (0803921C) dereferences a dangling pool word
    (0xB5100813, the next function's prologue) and would data-abort, so
    scale != 0 is a crash; all 72 blobs in this ROM have scale 0
    (asserted per blob at export)."""
    return min((native * q12_mult(semis)) >> 12, PCM_RATE_MAX)

# ---------------------------------------------------------------- helpers --
def _s8(v):
    return ((v + 128) % 256) - 128

def lfsr_clock(v):
    """GBA NR43 byte -> LFSR clock in Hz (5.24 MHz / divisor / 2^(shift+1))."""
    div = v & 7
    if div == 0:
        div = 0.5
    return 524288.0 / div / (2.0 ** (((v >> 4) & 0xF) + 1))

def lfsr_octaves(v, v_ref):
    """Pitch step of a drum-macro retrigger, in semitones (clamped)."""
    a, b = lfsr_clock(v), lfsr_clock(v_ref)
    if a <= 0 or b <= 0:
        return 0
    st = int(round(12.0 * math.log2(a / b)))
    return max(-24, min(24, st))

def s8b(v):
    v &= 0xFF
    return v - 256 if v >= 128 else v

def xm_note(gba_note):
    """GBA note byte -> XM note number (sample relative notes make it exact)."""
    return max(1, min(96, gba_note - 23))

def wave_samples(h):
    out = []
    for i in range(4):
        raw = h.rom.blob(h.WAVE_TABLE + 16 * i, 16)
        nums = [n for b in raw for n in (b >> 4, b & 15)]
        out.append([_s8((n - 8) * 16) for n in nums])
    return out

def square_sample(duty):
    steps = (4, 8, 16, 24)[duty]
    return [_s8(v) for v in ([100] * steps + [-100] * (32 - steps))]

def noise_sample(nr43=0):
    lfsr = 0x7FFF; out = []
    for _ in range(127 if nr43 & 8 else 32767):
        bit = ((lfsr >> 0) ^ (lfsr >> 1)) & 1
        lfsr = (lfsr >> 1) | (bit << 14)
        if nr43 & 8:
            lfsr = (lfsr & ~(1 << 6)) | (bit << 6)
        out.append(-100 if (lfsr & 1) else 100)
    return [_s8(v) for v in out]

def xm_pitch_fields(native_rate, base_note, gba_ref_note):
    """relative note + finetune so that XM note xm_note(gba_ref_note) plays at
    native_rate, and every other note scales by 2^(n/12) from there."""
    semis = 12.0 * math.log2(native_rate / 8363.0)
    want = semis - (xm_note(gba_ref_note) - 49)
    rel = int(round(want))
    ft = int(round((want - rel) * 128))
    if ft > 127: rel += 1; ft -= 128
    if ft < -128: rel -= 1; ft += 128
    return max(-100, min(100, rel)), max(-128, min(127, ft))

# ------------------------------------------------------------- XM writer ---
class XM:
    def __init__(self, name, n_channels, n_instruments):
        self.name = name; self.ch = n_channels; self.ins = n_instruments
        self.patterns = []      # {'rows': n, 'cells': {(row,ch): (note,ins,vol,fx,param)}}
        self.samples = []
        self.orders = []
        self.speed = 1          # ticks per row
        self.bpm = 149          # 149*0.4 = 59.6 rows/s ~= 59.7275 Hz
        self.restart = 0

    def pattern(self, rows):
        p = {'rows': rows, 'cells': {}}
        self.patterns.append(p)
        return p

    def pack_pattern(self, p):
        out = bytearray()
        rows = p['rows']
        for r in range(rows):
            ch = 0
            while ch < self.ch:
                c = p['cells'].get((r, ch))
                if c is None:
                    run = 1
                    while ch + run < self.ch and (r, ch + run) not in p['cells']:
                        run += 1
                    for _ in range(run):
                        out.append(0x80)
                    ch += run
                    continue
                note, ins, vol, fx, param = c
                mask = 0
                if note: mask |= 1
                if ins: mask |= 2
                if vol: mask |= 4
                if fx: mask |= 8 | 16
                out.append(0x80 | mask)
                if note: out.append(note)
                if ins: out.append(ins)
                if vol: out.append(vol)
                if fx:
                    out.append(fx)
                    out.append(param if param is not None else 0)
                ch += 1
        return bytes(out)

    def write(self, path):
        hdr = bytearray()
        hdr += b'Extended Module: '
        hdr += self.name.encode()[:20].ljust(20, b'\0')
        hdr += b'\x1a'
        hdr += b'Hachiemon extractor'.ljust(20, b'\0')
        hdr += struct.pack('<H', 0x0104)
        hdr += struct.pack('<I', 276)
        hdr += struct.pack('<H', len(self.orders))
        hdr += struct.pack('<H', self.restart)
        hdr += struct.pack('<H', self.ch)
        hdr += struct.pack('<H', len(self.patterns))
        hdr += struct.pack('<H', len(self.samples))
        hdr += struct.pack('<H', 1)
        hdr += struct.pack('<H', self.speed)
        hdr += struct.pack('<H', self.bpm)
        hdr += bytes(self.orders).ljust(256, b'\0')
        out = bytearray(hdr)
        for p in self.patterns:
            data = self.pack_pattern(p)
            out += struct.pack('<I', 9)
            out += bytes([0])
            out += struct.pack('<H', p['rows'])
            out += struct.pack('<H', len(data))
            out += data
        for s in self.samples:
            ih = bytearray()
            ih += struct.pack('<I', 263)
            ih += s['name'].encode()[:22].ljust(22, b'\0')
            ih += bytes([0])
            ih += struct.pack('<H', 1)
            ih += struct.pack('<I', 40)
            ih += bytes(96)
            ih += bytes(48)
            ih += bytes(48)
            ih += bytes(14)
            ih += struct.pack('<H', 0)
            ih += bytes(22)
            assert len(ih) == 263, len(ih)
            out += ih
            data = s['data']
            sh = bytearray()
            sh += struct.pack('<I', len(data))
            sh += struct.pack('<I', s['loop_start'])
            sh += struct.pack('<I', s['loop_len'])
            sh += struct.pack('<B', s['vol'])
            sh += struct.pack('<b', s['ft'])
            sh += bytes([s['type']])
            sh += bytes([s['pan']])
            sh += struct.pack('<b', s['rel'])
            sh += bytes([0])
            sh += s['name'].encode()[:22].ljust(22, b'\0')
            assert len(sh) == 40, len(sh)
            out += sh
            d = bytearray(); prev = 0
            for b in data:
                sb = b - 256 if b > 127 else b
                d.append((sb - prev) & 0xFF)
                prev = sb
            out += d
        open(path, 'wb').write(out)
        return len(out)

SEQ_OP_SIZE = [1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0,   # E0-EF
           1, 0, 1, 0, 4, 4, 0, 1, 1, 2, 1, 4, 0, 0, 1, 0]   # F0-FF

WAVE_LEVELS = [0, 25, 25, 25, 50, 50, 50, 50, 75, 75, 75, 75, 100, 100, 100, 100]

def vol_column(engine, nibble):
    """Driver volume to XM column: PSG nibble, wave LUT, or DMA 50/100%.

PSG sample amplitudes encode the mixer ratio: its full-scale range is 240 DAC
units, while 50% DMA spans 512. Preview gain is separate from these ratios.
"""
    nibble = max(0, min(15, nibble))
    if engine in (4, 5):
        if nibble == 0:
            return 0x10
        return 0x30 if nibble < 8 else 0x50
    if engine == 2:
        pct = WAVE_LEVELS[nibble]
    else:
        pct = nibble * 100 / 15
    return 0x10 + int(round(pct * 64 / 100))

# ------------------------------------------------------- driver simulation -
class Sim:
    """Frame-exact simulation of one driver channel record (0x64 bytes)."""

    MAX_TICKS = 400000

    def __init__(self, h, slot, engine, ptr):
        self.h = h; self.rom = h.rom
        self.slot = slot; self.engine = engine; self.ptr = ptr
        self.fnum = [self.rom.u16(FNUM_TAB + 2 * i) for i in range(74)]
        # NOTE: 080388F0 reads the table with NO index clamp, so the
        # vibrato span f(n+2)-f(n) over-reads 2 words past the table end
        # for n in {70, 71}; entries 72-73 are the raw following ROM
        # words (entry 73 overlaps WAVE_LUT), read verbatim.
        # stream state
        self.pc = ptr; self.tick = 0; self.dur = 0
        self.latch62 = 0        # +0x62 duration high byte (FA)
        self.mode = 0           # +0x57 (FE)
        self.transpose = 0      # +0x5E (E8)
        self.instr = 0          # +0x5B (E3 / E4-E7)
        self.tbl63 = 3          # +0x63 (EA, default 3)
        self.enable = 0         # +0x5F (E9)
        self.f8 = 0             # +0x49 (F8)
        self.durbase = 0        # +0x24 (E1)
        self.p5d = 0            # +0x5D (E2)
        self.retrig = False     # flag 0x10 pending
        # volume / ADSR
        self.vol59 = 0x0F; self.vol = 0
        self.adsr = [0, 0, 0, 0]
        self.adsr_on = False
        self.st = 0; self.ctr = 0; self.step = 0
        # vibrato
        self.vib_a = 0; self.vib_lo = 0; self.vib_hi = 0
        self.v_delta = 0; self.v_acc = 0; self.v_delay = 0; self.v_per = 0
        # drum macro
        self.macro = 0; self.m_timer = 0
        self.drum = 0; self.drum_p0 = None
        # note state
        self.note = None        # current note index (engines 0-2) / pitch byte
        self.key_on = False
        self.loops = {}
        self.ret = 0
        # outputs
        self.notes = []         # (tick, tag, payload, dur)
        self.vols = {0: 0}
        self.pitch = {}         # tick -> effective fnum (engines 0-2)
                                #      -> effective DMA rate Hz (engines 4-5)
        self.pcm_base_rate = None  # R at PCM note start (08039290)
        self.notevols = {}      # tick -> vol at PCM note start
        self.pcm_dur_eff = {}   # tick -> voice life in ticks (+0x18)
        self.cuts = []          # (kill_tick, src_note_tick) PCM kills (08039468)
        self.end_tick = 0
        self.loop_start = None; self.loop_end = None
        self.trace = []         # debug: (tick, 'op', ...)
        self.frames = []
        self.history = [255] * 48
        self.current_fnum = 0
        self.raw_pitch = 255
        self.sweep = 8
        self.last_echo_note = 255
        self.heard_vol = 0
        self.heard_fnum = 0
        self.dirty = 0x5F
        self.commands = []

    # ------------------------------------------------------------- stream --
    def _u8(self):
        v = self.rom.u8(self.pc); self.pc += 1; return v

    def _u32(self):
        v = self.rom.u32(self.pc); self.pc += 4; return v

    def gate(self):
        """080376A4 - true when the release window has been reached."""
        if (self.adsr[2] & 0xFF) >= self.dur:
            self.t5_setup(); self.t5_tick()
            return True
        return False

    # -- ADSR ---------------------------------------------------------------
    def t1_setup(self):
        if not self.gate():
            self.ctr = 1; self.step = 0; self.st = 1

    def t2_setup(self):
        if not self.gate():
            rate = (self.adsr[0] >> 4) & 0xF
            if rate:
                self.ctr = rate; self.step = self.adsr[0] & 0xF; self.st = 2
            else:
                self.t3_setup()

    def t3_setup(self):
        if not self.gate():
            rate = (self.adsr[1] >> 4) & 0xF
            if rate:
                self.ctr = rate; self.step = (-(self.adsr[1] & 0xF)) & 0xFF; self.st = 3
            else:
                self.sustain_setup()

    def sustain_setup(self):
        if not self.gate():
            if (self.adsr[2] & 0xFF) == 0:
                self.t5_setup()
            else:
                self.st = 4; self.ctr = self.adsr[2] & 0xFF

    def t5_setup(self):
        if (self.adsr[3] >> 4) == 0:
            self.env_end()
        else:
            self.ctr = 1; self.step = self.adsr[3] & 0xF; self.st = 5

    def env_end(self):
        self.st = 0; self._set_vol(0)

    def adsr_tick(self):
        if not self.adsr_on:
            return
        st = self.st
        if st == 1:
            self.ctr = (self.ctr - 1) & 0xFF
            if self.ctr == 0:
                self.t2_setup()
        elif st == 2:
            self.ctr = (self.ctr - 1) & 0xFF
            if self.ctr == 0xFF:
                self.t3_setup()
            else:
                self._set_vol(min(15, self.vol + s8b(self.step)))
        elif st == 3:
            self.ctr = (self.ctr - 1) & 0xFF
            if self.ctr == 0xFF:
                self.sustain_setup()
            else:
                self._set_vol(max(0, self.vol + s8b(self.step)))
        elif st == 4:
            self.gate()
        elif st == 5:
            self.t5_tick()

    def t5_tick(self):
        self.ctr = (self.ctr - 1) & 0xFF
        if self.ctr != 0:
            return
        step = self.step & 0xFF
        if s8b(step) == 0:
            self.env_end(); return
        self.step = (step - 1) & 0xFF
        self._set_vol(max(0, self.vol - 1))
        self.ctr = (self.adsr[3] >> 4) & 0xFF

    def _set_vol(self, v):
        self.dirty |= 2
        if v != self.vol:
            self.vol = v
            self.vols[self.tick] = v

    # -- vibrato ------------------------------------------------------------
    def vib_setup(self):
        self.v_delay = self.vib_a & 0xFF
        self.v_per = (self.vib_lo >> 1) & 0xFF
        n = self.note
        span = 0
        if n is not None and 0 <= n < 72:
            # 08037900: span = u16(f(n+2)) - u16(f(n)), 32-bit wrap-exact
            span = (self.fnum[n + 2] - self.fnum[n]) & 0xFFFFFFFF
        if self.v_per:
            self.v_delta = ((span * self.vib_hi) & 0xFFFFFFFF) // self.v_per
        else:
            # 08039C44 with r1 == 0 returns 0 (08039CB2)
            self.v_delta = 0
        self.v_acc = 0

    def pcm_vib_setup(self, raw_pitch, native, base):
        """08037928: PCM vibrato setup.

        The span uses [+0x40] WITHOUT the transpose (0803796E reads the
        raw pitch byte while the voice-start rate folds +0x5E), so deep
        vibrato on a transposed channel is scaled by the transpose ratio.
        With vib_hi == 0 the hardware skips setup, but the tick is dead
        and the apply tail is gated too, so resetting here is equivalent
        and keeps the accumulator exact."""
        self.v_delay = self.vib_a & 0xFF
        self.v_per = (self.vib_lo >> 1) & 0xFF
        span = (pcm_rate(native, raw_pitch + 2 - base)
                - pcm_rate(native, raw_pitch - base))
        if self.v_per:
            self.v_delta = (span * self.vib_hi) // self.v_per
        else:
            self.v_delta = 0
        self.v_acc = 0

    def vib_tick(self):
        if (self.vib_hi & 0xF) == 0 and self.vib_lo == 0:
            return
        if self.vib_hi == 0:
            return
        if self.v_delay:
            self.v_delay -= 1
            return
        self.v_acc = ((self.v_acc + self.v_delta + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        self.dirty |= 1
        if self.v_per:
            self.v_per -= 1
        else:
            self.v_per = self.vib_lo & 0xFF
            self.v_delta = -self.v_delta

    def eff_fnum(self):
        if self.engine not in (0, 1, 2):
            return None
        # 08038AD8/080389xx writers clamp the sounded value to [0, 0x7FF]
        value = self.current_fnum + self.f8 + (self.v_acc >> 4)
        value = ((value + 0x8000) & 0xFFFF) - 0x8000
        return max(0, min(0x7FF, value))

    def eff_pcm_rate(self):
        """Effective DirectSound rate: R + (acc >> 4) (080395AA)."""
        if self.engine not in (4, 5):
            return None
        if self.pcm_base_rate is None:
            return None
        return self.pcm_base_rate + (self.v_acc >> 4)

    # -- drum macro ---------------------------------------------------------
    def macro_step(self):
        if not self.macro:
            return
        b = self.rom.u8(self.macro); self.macro += 1
        if (b & 0xF0) == 0xD0:
            self.vol59 = b & 0x0F
            self._set_vol(b & 0x0F)
            self.m_timer = self.durbase & 0xFF
            self.retrig = True
        elif b == 0xE1:
            self.durbase = self.rom.u8(self.macro); self.macro += 1
        elif b == 0xE2:
            self.p5d = self.rom.u8(self.macro); self.macro += 1
            self.retrig = True
        elif b == 0xFF:
            self.vol59 = 0
            self._set_vol(0)
            self.macro = 0
        else:
            pass

    def macro_tick(self):
        if not self.macro:
            return
        if self.m_timer:
            self.m_timer -= 1
        n = 0
        while self.macro and self.m_timer == 0 and n < 64:
            self.macro_step(); n += 1

    # -- command fetch ------------------------------------------------------
    def fetch_one(self):
        """Process one stream command (may or may not set self.dur)."""
        if not self.rom.ok(self.pc):
            self.pc = 0; self.dur = 0; self.key_on = False
            self.loop_end = self.tick; self.end_tick = self.tick
            raise StopIteration
        b = self._u8()
        if b < 0x80:
            self.cmd_note(b)
        elif b < 0x90 or b == 0xE0:
            d = self._u8()
            self.cmd_note_off(d)
        elif b < 0xD0:
            pass                                    # 0x90-0xCF: ignored
        elif b < 0xE0:
            self.vol59 = b & 0x0F
            self._set_vol(b & 0x0F)
            if self.engine == 3:
                self.retrig = True
        else:
            self.cmd_extended(b)

    def _note_common(self):
        """Duration + mode handling shared by all note forms."""
        d = self._u8()
        dur = ((self.latch62 << 8) | d) + 1
        self.latch62 = 0
        self.dur = dur
        return dur

    def cmd_note(self, b):
        self.last_echo_note = 255
        self.dirty |= 0x11
        self._set_vol_from_59()
        if self.mode == 1:
            # legato: duration comes from [+0x24]; engines 0-2,4,5 read one byte
            self.dur = max(1, self.durbase)
            if self.engine in (0, 1, 2, 4, 5) and self.rom.ok(self.pc):
                extra = self._u8()
                if self.engine <= 2:
                    self.current_fnum = (b << 8) | extra
                    self.note = 0
                    self.raw_pitch = 0
                    self.key_on = True
                    self.notes.append((self.tick, 'psg' if self.engine < 2 else 'wave',
                                       0x24, self.dur, self.instr))
                    return
            self.note_common_engine(b, self.dur)
        else:
            dur = self._note_common()
            self.note_common_engine(b, dur)

    def note_common_engine(self, pitch, dur):
        eng = self.engine
        if eng in (0, 1, 2):
            idx = max(0x24, min(0x6B, pitch + self.transpose))
            self.note = idx - 0x24
            self.raw_pitch = self.note
            self.current_fnum = self.fnum[self.note]
            self.key_on = True
            self.notes.append((self.tick, 'psg' if eng in (0, 1) else 'wave',
                               idx, dur, self.instr))
            self.vib_setup()
            self.t1_setup()
        elif eng == 3:
            if self.mode != 2:
                self.key_on = True
                self.retrig = True
                return
            # percussion: drum index = clamp(pitch - base, 0, count-1)
            rom = self.rom
            cnt = rom.u16(DRUM_TAB); base = rom.u16(DRUM_TAB + 2)
            drum = max(0, min(cnt - 1, pitch - base))
            self.note = drum
            self.key_on = True
            self.drum = drum
            self.drum_p0 = None
            self.macro = rom.u32(DRUM_TAB + 4 + 4 * drum)
            self.m_timer = 0
            self.retrig = True
        else:
            if self.mode == 2:
                cnt = self.rom.u16(DRUM_TAB); base = self.rom.u16(DRUM_TAB + 2)
                drum = max(0, min(cnt - 1, pitch - base))
                pitch_hw = 0x23 + drum       # no transpose on mode 2
                raw40 = drum                 # [+0x40] (F9-on-pcm2 absent here)
                tab, idx = 2, drum
                tag, pay = 'pcm2', drum
            else:
                # 08038EBA: voice-start rate folds the SIGNED transpose
                pitch_hw = pitch + self.transpose
                raw40 = pitch                # span quirk: [+0x40] unfolded
                tab = 1
                idx = self.instr if self.instr < 64 else 0
                tag, pay = 'pcm1', (idx, pitch_hw)
            if self.vol == 0:
                # 08038E52: vol==0 skips DMA start AND vib setup; the
                # old voice is killed (08039468) and nothing restarts.
                self.cuts.append((self.tick, self.tick))
                self.note = None
                self.key_on = True
                return
            # 08038E5C: voice life +0x18 = dur, or dur-release when the
            # ADSR enable bit is set (FB/FC), minimum 1 tick. The age
            # kill (08039596) observably lands one frame AFTER the age
            # threshold is reached (commit/DMA-IRQ phasing vs the game
            # sound update), so the AUDIBLE length is +0x18 + 1: FB
            # (release 1, all songs) cuts exactly at note end; non-FB
            # rings one tick past note end (extrapolated: no non-FB
            # capture exists yet). Verified on 164 song10 FB windows
            # (best-lag RMS: ring 1.62 dB vs early-cut 1.82 dB).
            if self.adsr_on and dur > self.adsr[3]:
                dur_eff = dur - self.adsr[3]
            elif self.adsr_on:
                dur_eff = 1
            else:
                dur_eff = dur
            audible = dur_eff + 1
            _size, native, base_b, _loop, _lps = blob_meta(self.h, tab, idx)
            self.pcm_base_rate = pcm_rate(native, pitch_hw - base_b)
            self.pcm_vib_setup(raw40, native, base_b)
            self.note = pitch_hw if self.mode != 2 else drum
            self.key_on = True
            self.cuts.append((self.tick + audible, self.tick))
            self.pcm_dur_eff[self.tick] = audible
            self.notevols[self.tick] = self.vol
            self.notes.append((self.tick, tag, pay, dur, self.instr))

    def _set_vol_from_59(self):
        self._set_vol(self.vol59)
        self.vols[self.tick] = self.vol

    def cmd_note_off(self, d):
        self.last_echo_note = 255
        self.dirty |= 1
        self.dur = d + 1
        self.key_on = False
        self.note = None
        self.raw_pitch = 255
        self.current_fnum = 0
        self._set_vol(0)
        if self.engine == 3:
            self.retrig = True

    def cmd_extended(self, b):
        n = SEQ_OP_SIZE[b - 0xE0]
        args = [self._u8() for _ in range(n)]
        self.commands.append((self.tick, b, args))
        if b == 0xE1:
            self.durbase = args[0]
        elif b == 0xE2:
            self.p5d = args[0]
            self.retrig = True
        elif b == 0xE3:
            self.instr = args[0]
        elif b in (0xE4, 0xE5, 0xE6, 0xE7):
            self.instr = (b - 0xE4) << 6
        elif b == 0xE8:
            self.transpose = s8b(args[0])
        elif b == 0xE9:
            self.enable = min(48, args[0])
        elif b == 0xEA:
            self.tbl63 = args[0]
        elif b == 0xF0:
            self.loops[0] = (self.pc, args[0])
        elif b == 0xF1:
            if 0 in self.loops:
                pos, cnt = self.loops[0]
                if cnt > 1:
                    self.loops[0] = (pos, cnt - 1); self.pc = pos
                else:
                    del self.loops[0]
        elif b == 0xF2:
            self.loops[1] = (self.pc, args[0])
        elif b == 0xF3:
            if 1 in self.loops:
                pos, cnt = self.loops[1]
                if cnt > 1:
                    self.loops[1] = (pos, cnt - 1); self.pc = pos
                else:
                    del self.loops[1]
        elif b == 0xF4:
            t = args[0] | args[1] << 8 | args[2] << 16 | args[3] << 24
            if t <= self.pc:
                if self.loop_end is None:
                    self.loop_end = self.tick
                    self.loop_start = self.visited.get(t, 0)
                if self.until is None:
                    self.end_tick = self.tick
                    raise StopIteration
            self.pc = t
        elif b == 0xF5:
            self.ret = self.pc
            self.pc = args[0] | args[1] << 8 | args[2] << 16 | args[3] << 24
        elif b == 0xF6:
            if self.ret:
                self.pc, self.ret = self.ret, 0
            else:
                raise StopIteration
        elif b == 0xF7:
            self.sweep = args[0] or 8
        elif b == 0xF8:
            self.f8 = args[0]
        elif b == 0xF9:
            self.vib_a = args[0]
            self.vib_lo = args[1] & 0x0F
            self.vib_hi = args[1] >> 4
        elif b == 0xFA:
            self.latch62 = args[0]
        elif b == 0xFB:
            self.adsr = list(args)
            self.adsr_on = True
            if self.engine <= 2:
                self.t1_setup()
        elif b == 0xFC:
            self.adsr_on = True
            if self.engine <= 2:
                self.t1_setup()
        elif b == 0xFD:
            self.adsr_on = False
            if self.engine <= 2:
                self.t1_setup()
        elif b == 0xFE:
            self.mode = args[0]
        elif b == 0xFF:
            self._set_vol(0)
            self.key_on = False
            self.end_tick = self.tick
            raise StopIteration

    # ---------------------------------------------------------------- run --
    def run(self, until=None):
        self.visited = {}
        self.until = until
        try:
            while self.tick < (until if until is not None else self.MAX_TICKS):
                self.latch62 = 0
                if self.dur:
                    self.dur -= 1
                n = 0
                while self.dur == 0 and n < 10000:
                    self.visited.setdefault(self.pc, self.tick)
                    self.fetch_one()
                    n += 1
                # per-frame modulation, in driver order (fetch, mods, apply)
                if self.engine in (0, 1):
                    self.vib_tick(); self.adsr_tick()
                elif self.engine == 2:
                    self.vib_tick(); self.adsr_tick()
                elif self.engine == 3:
                    self.macro_tick()
                    if self.retrig and self.vol:
                        self.notes.append((self.tick, 'noise', (self.drum, self.p5d), 1, self.instr))
                    self.retrig = False
                else:
                    self.vib_tick()  # 08038244: PCM voice tick is vib-only
                f = self.eff_fnum()
                if f is not None:
                    self.pitch[self.tick] = f
                r = self.eff_pcm_rate()
                if r:
                    r = 16777216 / max(1, 16777216 // min(PCM_RATE_MAX, r))
                if r is not None:
                    self.pitch[self.tick] = r
                heard_vol, heard_fnum, echo = self.vol, f, False
                if self.engine <= 2:
                    heard_vol, heard_fnum = self.heard_vol, self.heard_fnum
                    if self.vol == 0 and self.enable:
                        old = self.history[(self.tick - self.enable) % 48]
                        heard_vol, heard_fnum = self.heard_vol, self.heard_fnum
                        if old != self.last_echo_note:
                            self.last_echo_note = old
                            heard_vol = 3 if old != 255 else 0
                            if old != 255:
                                value = ((self.fnum[old] + self.f8 + 0x8000) & 0xFFFF) - 0x8000
                                heard_fnum = max(0, min(0x7FF, value))
                        echo = bool(heard_vol)
                    else:
                        if self.dirty & (2 if self.engine == 2 else 6):
                            heard_vol = self.vol
                        if self.dirty & 1:
                            heard_fnum = f
                        elif self.engine < 2 and self.dirty & 0x12:
                            heard_fnum = self.current_fnum
                        self.dirty = 0
                    self.heard_vol, self.heard_fnum = heard_vol, heard_fnum
                    self.history[self.tick % 48] = self.raw_pitch if self.vol else 255
                self.frames.append(dict(vol=heard_vol, pitch=heard_fnum if self.engine <= 2 else r,
                                        instr=self.instr, echo=echo, st=self.st, ctr=self.ctr,
                                        step=self.step, sweep=self.sweep, nr43=self.p5d,
                                        dur=self.dur, pc=self.pc, raw_vol=self.vol))
                self.tick += 1
        except StopIteration:
            pass
        self.end_tick = max(self.end_tick, self.tick)
        return self

# ------------------------------------------------------------------ build --
def blob_meta(h, tab, i):
    p = h.rom.u32((SEQ_TAB1 if tab == 1 else SEQ_TAB2) + 4 * i)
    size = h.rom.u32(p); rate = h.rom.u16(p + 4); scale = h.rom.u8(p + 6)
    base = h.rom.u8(p + 7); loop = h.rom.u32(p + 8); lps = h.rom.u32(p + 0xC)
    assert scale == 0, 'blob tab%d[%d] scale=%d (0803921C would crash)' % (tab, i, scale)
    return size, rate, base, loop, lps

def blob(h, tab, i):
    p = h.rom.u32((SEQ_TAB1 if tab == 1 else SEQ_TAB2) + 4 * i)
    size, rate, base, loop, lps = blob_meta(h, tab, i)
    return list(h.rom.blob(p + 0x10, size)), rate, base, loop, lps

def build(h, song_idx, out_path=None, verbose=False, write_mapping=True):
    assert [h.rom.u16(Q12_TAB_ADDR + 2 * i) for i in range(12)] == Q12_TAB
    song = h.song(song_idx)
    initial = [Sim(h, c['slot'], c['engine'], c['stream']).run() for c in song['channels']]
    loops = [(s.loop_start, s.loop_end) for s in initial if s.loop_start is not None]
    loop_start = max(a for a, b in loops) if loops else None
    total_rows = loop_start + math.lcm(*(b - a for a, b in loops)) if loops else max(s.end_tick for s in initial) + 1
    if total_rows > 65536:
        raise ValueError('The combined channel loop exceeds the XM order-table capacity')
    sims = [Sim(h, c['slot'], c['engine'], c['stream']).run(total_rows) for c in song['channels']]
    for sim in sims:
        for tick, command, arguments in sim.commands:
            if command == 0xF7 and arguments[0] & 7:
                raise NotImplementedError('Active hardware sweep at slot %d tick %d' % (sim.slot, tick))
            if command == 0xEA and arguments[0] != 3:
                raise NotImplementedError('Non-stereo EA routing at slot %d tick %d' % (sim.slot, tick))
    samples, idx_of = [], {}

    def add_sample(key, name, data, rate, ref_note, loop=None, vol=64):
        if key not in idx_of:
            if vol != 64:
                data = [int(round(s8b(value) * vol / 64)) for value in data]
            rel, ft = xm_pitch_fields(rate, ref_note, ref_note)
            ls, ll = loop or (0, 0)
            if loop and not (0 <= ls < len(data) and 0 < ll <= len(data) - ls):
                raise ValueError('Invalid sample loop: ' + name)
            samples.append(dict(name=name, data=data, rel=rel, ft=ft, base=ref_note,
                                loop_start=ls, loop_len=ll, type=int(bool(loop)), vol=64, pan=128))
            idx_of[key] = len(samples)
        return idx_of[key]

    waves = wave_samples(h)
    psg_lengths = {}
    pcm_peaks = {}
    for sim in sims:
        if sim.engine <= 2:
            peak = max(((65536 if sim.engine == 2 else 131072) / (2048 - f['pitch'])
                        for f in sim.frames if f['vol']), default=1)
            length = 512
            while peak * length > 400000 and length > 32:
                length //= 2
            psg_lengths[sim.engine] = min(psg_lengths.get(sim.engine, length), length)
        elif sim.engine >= 4:
            pcm_peaks[sim.engine] = max((f['pitch'] or 0 for f in sim.frames), default=1)

    def instrument(engine, shape, tag=None, payload=None):
        if engine < 2:
            duty = (shape >> 6) & 3
            length = psg_lengths[engine]
            if ('sq', duty, length) in idx_of:
                return idx_of['sq', duty, length]
            data = [value for value in square_sample(duty) for _ in range(length // 32)]
            return add_sample(('sq', duty, length), 'pulse %s%%' % (12.5, 25, 50, 75)[duty],
                              data, 8372.0 * length / 32, 60, (0, length), 19.2)
        if engine == 2:
            if not 0 <= shape < len(waves):
                raise ValueError('Wave RAM index outside the ROM bank: %d' % shape)
            length = psg_lengths[engine]
            if ('wv', shape, length) in idx_of:
                return idx_of['wv', shape, length]
            data = [value for value in waves[shape] for _ in range(length // 32)]
            data = [int(round((s8b(value) + 8) * 0.5)) for value in data]
            return add_sample(('wv', shape, length), 'wave RAM %d' % shape, data, 8372.0 * length / 32, 60, (0, length), 32)
        if engine == 3:
            nr43 = payload[1]
            if ('noise', nr43) in idx_of:
                return idx_of['noise', nr43]
            data = noise_sample(nr43)
            rate = lfsr_clock(nr43)
            if rate > 32768 and rate % 32768 == 0:
                stride = int(rate / 32768)
                data = [data[(i * stride) % len(data)] for i in range(len(data))]
                rate = 32768
            hold = max(1, min(8, int(400000 / rate)))
            data = [value for value in data for _ in range(hold)]
            return add_sample(('noise', nr43), 'noise NR43=%02X %db' % (nr43, 7 if nr43 & 8 else 15),
                              data, rate * hold, 72, (0, len(data)), 19.2)
        bank, index = (1, payload[0]) if tag == 'pcm1' else (2, payload)
        data, rate, base, loop, lps = blob(h, bank, index)
        peak = pcm_peaks.get(engine, rate)
        hold = max(1, min(8, int(400000 / max(rate, peak))))
        if ('pcm', bank, index, hold) in idx_of:
            return idx_of['pcm', bank, index, hold]
        data = [value for value in data for _ in range(hold)]
        ident = add_sample(('pcm', bank, index, hold), 'pcm%d[%d]' % (bank, index), data, rate * hold, base,
                           (lps * hold, len(data) - lps * hold) if loop else None)
        samples[ident - 1]['rate_multiplier'] = hold
        return ident

    xm = XM('Monsters Inc %02d' % song_idx, len(sims) + 2, 0)
    boundaries = sorted(set([0, total_rows] + list(range(256, total_rows, 256)) + ([loop_start] if loop_start else [])))
    xm.patterns = [{'rows': b - a, 'cells': {}} for a, b in zip(boundaries, boundaries[1:])]
    xm.orders = list(range(len(xm.patterns)))
    if len(xm.orders) > 256:
        raise ValueError('Too many XM patterns')
    if loop_start is not None:
        xm.restart = boundaries.index(loop_start)

    def put(tick, channel, cell):
        pattern = bisect_right(boundaries, tick) - 1
        xm.patterns[pattern]['cells'][(tick - boundaries[pattern], channel)] = tuple(cell)

    stats = dict(source_notes=0, exported_notes=0, noise_retriggers=0, echo_rows=0,
                 pitch_effects=0, volume_cells=0, max_pitch_error_cents=0.0)
    mapping = []
    for ci, sim in enumerate(sims):
        events = {ev[0]: ev for ev in sim.notes}
        kills = {t: src for t, src in sim.cuts}
        starts = sorted(t for t, tag, pay, dur, ins in sim.notes if tag in ('pcm1', 'pcm2'))
        stats['source_notes'] += len(sim.notes)
        current_ins = current_note = current_start = None
        emitted = 0.0
        previous_volume = None
        previous_echo = False
        previous_pitch = None
        pcm_alive = False
        for tick in range(total_rows):
            frame = sim.frames[tick] if tick < len(sim.frames) else dict(vol=0, pitch=None, instr=0, echo=False)
            event = events.get(tick)
            cell = [0, 0, 0, 0, 0]
            eng = sim.engine
            if event:
                _, tag, payload, dur, shape = event
                current_ins = instrument(eng, shape, tag, payload)
                if eng <= 2:
                    current_note = xm_note(payload) - (12 if eng == 2 else 0)
                elif eng == 3:
                    current_note = xm_note(72)
                    stats['noise_retriggers'] += 1
                else:
                    current_note = xm_note(payload[1] if tag == 'pcm1' else 35 + payload)
                    pcm_alive = True
                current_note = max(1, min(96, current_note))
                if eng <= 2 and frame['echo']:
                    sample = samples[current_ins - 1]
                    rate = (65536.0 if eng == 2 else 131072.0) * len(sample['data']) / (2048 - frame['pitch'])
                    current_note = max(1, min(96, int(round(49 + 12 * math.log2(rate / 8363) - sample['rel'] - sample['ft'] / 128))))
                current_start = tick
                cell[:2] = [current_note, current_ins]
                emitted = 0.0
            elif eng <= 2 and frame['vol'] and (frame['echo'] != previous_echo or
                    (frame['echo'] and frame['pitch'] != previous_pitch) or
                    current_ins is None or samples[current_ins - 1]['name'] !=
                    ('wave RAM %d' % frame['instr'] if eng == 2 else 'pulse %s%%' % (12.5, 25, 50, 75)[(frame['instr'] >> 6) & 3])):
                current_ins = instrument(eng, frame['instr'])
                freq = (65536.0 if eng == 2 else 131072.0) / (2048 - frame['pitch'])
                current_note = max(1, min(96, int(round(49 + 12 * math.log2(freq * len(samples[current_ins - 1]['data']) / 8363) -
                                             samples[current_ins - 1]['rel'] - samples[current_ins - 1]['ft'] / 128))))
                cell[:2] = [current_note, current_ins]
                emitted = 0.0
            if eng in (4, 5) and tick in kills:
                src = kills[tick]
                next_index = bisect_right(starts, src)
                if next_index == len(starts) or starts[next_index] > tick:
                    pcm_alive = False
            level = frame['vol'] if eng <= 3 else (sim.notevols.get(current_start, 0) if pcm_alive else 0)
            volume = vol_column(eng, level)
            if cell[0] or volume != previous_volume:
                cell[2] = volume
                stats['volume_cells'] += 1
            previous_volume = volume
            if frame['echo']:
                stats['echo_rows'] += 1
            previous_echo = frame['echo']
            previous_pitch = frame['pitch']
            if current_ins is not None and eng != 3 and frame['pitch'] is not None and level:
                sample = samples[current_ins - 1]
                rate0 = 8363.0 * 2 ** ((current_note - 49 + sample['rel'] + sample['ft'] / 128.0) / 12)
                if eng >= 4:
                    target = frame['pitch'] * sample['rate_multiplier']
                else:
                    target = (65536.0 if eng == 2 else 131072.0) * len(sample['data']) / (2048 - frame['pitch'])
                want = 768.0 * math.log2(target / rate0)
                difference = want - emitted
                if abs(difference) >= 0.5:
                    if abs(difference) <= 15:
                        amount = max(1, min(15, int(round(abs(difference)))))
                        cell[3:] = [0x21, (0x10 if difference > 0 else 0x20) | amount]
                        emitted += amount if difference > 0 else -amount
                    else:
                        amount = max(1, min(15, int(round(abs(difference) / 4))))
                        cell[3:] = [0x0E, (0x10 if difference > 0 else 0x20) | amount]
                        emitted += 4 * amount if difference > 0 else -4 * amount
                    stats['pitch_effects'] += 1
                stats['max_pitch_error_cents'] = max(stats['max_pitch_error_cents'], abs(want - emitted) * 100 / 64)
            if cell[0]:
                stats['exported_notes'] += 1
            if any(cell):
                put(tick, ci, cell)
        mapping.append(dict(channel=ci, slot=sim.slot, engine=sim.engine,
                            loop_start=initial[ci].loop_start, loop_end=initial[ci].loop_end,
                            adsr='frame volume column', lfo='tick-zero fine portamento',
                            echo='E9 delayed note history' if sim.engine <= 2 else None,
                            emitted_source_events=len(sim.notes),
                            source_commands=sorted({hex(command) for tick, command, arguments in sim.commands})))

    elapsed = 0.0
    previous_bpm = xm.bpm
    for tick in range(total_rows):
        target_time = (tick + 1) / GBA_TICK_HZ
        bpm = min((125, 150), key=lambda value: abs(elapsed + 2.5 / value - target_time))
        if bpm != previous_bpm:
            put(tick, len(sims), [0, 0, 0, 0x0F, bpm])
        elapsed += int(44100 * 2.5 / bpm) / 44100
        previous_bpm = bpm
    if loop_start is not None:
        put(total_rows - 1, len(sims) + 1, [0, 0, 0, 0x0B, xm.restart])
    xm.samples = samples
    if len(samples) > 128:
        raise ValueError('Too many XM instruments')
    size = xm.write(out_path) if out_path else 0
    result = dict(rows=total_rows, patterns=len(xm.patterns), channels=xm.ch, samples=len(samples),
                  loop=loop_start, seconds=elapsed, timing_error_ms=(elapsed - total_rows / GBA_TICK_HZ) * 1000,
                  timing_player='125/150 BPM ticks integral at both 44100 and 48000 Hz',
                  sample_model='expanded sample holds; integer-ratio fast noise decimated to 32768 Hz DAC',
                  fidelity='driver state verified; combined DAC clipping, phase and PWM remain approximate',
                  mapping=mapping, statistics=stats)
    if out_path and write_mapping:
        with open(os.path.splitext(out_path)[0] + '.mapping.json', 'w') as stream:
            json.dump(result, stream, indent=2)
    if verbose:
        print('song %02d: %d rows, %d instruments, %d bytes, loop=%s' %
              (song_idx, total_rows, len(samples), size, loop_start))
    return result

# ==========================================================================
# XM PLAYER / RENDERER
# ==========================================================================
"""Preview player for the generated XM subset; native players verify export."""

class Sample:
    pass

def xm_load(path):
    d = open(path, 'rb').read()
    assert d[:17] == b'Extended Module: '
    name = d[17:37].rstrip(b'\0').decode('latin1')
    hsize = struct.unpack_from('<I', d, 0x3C)[0]
    (songlen, restart, nch, npat, nins, flags, speed, bpm) = \
        struct.unpack_from('<8H', d, 0x40)
    orders = list(d[0x50:0x50 + 256])[:songlen]
    pos = 0x3C + hsize
    patterns = []
    for _ in range(npat):
        phsize, = struct.unpack_from('<I', d, pos)
        rows, psize = struct.unpack_from('<HH', d, pos + 5)
        pdata = d[pos + 9:pos + 9 + psize]
        pos += phsize + psize
        cells = [[(0, 0, 0, 0, 0)] * nch for _ in range(rows)]
        i = 0; r = 0; ch = 0
        while r < rows:
            x = pdata[i]; i += 1
            if x & 0x80:
                note = ins = vol = fx = par = 0
                if x & 1: note = pdata[i]; i += 1
                if x & 2: ins = pdata[i]; i += 1
                if x & 4: vol = pdata[i]; i += 1
                if x & 8: fx = pdata[i]; i += 1
                if x & 16: par = pdata[i]; i += 1     # parameter follows fx
                cells[r][ch] = (note, ins, vol, fx, par)
            else:
                note = x
                ins = pdata[i]; vol = pdata[i+1]; fx = pdata[i+2]; par = pdata[i+3]
                i += 4
                cells[r][ch] = (note, ins, vol, fx, par)
            ch += 1
            if ch == nch:
                ch = 0; r += 1
        patterns.append(cells)
    samples = []
    for _ in range(nins):
        isize, = struct.unpack_from('<I', d, pos)
        nsmp, = struct.unpack_from('<H', d, pos + 0x1B)
        pos += isize
        for _ in range(nsmp):
            (length, lstart, llen, vol, ft, stype, pan, rel, res) = \
                struct.unpack_from('<IIIBBBBBB', d, pos)
            nm = d[pos + 0x12:pos + 0x12 + 22].rstrip(b'\0').decode('latin1')
            pos += 40
            raw = d[pos:pos + length]; pos += length
            s = Sample()
            s.data = np.zeros(length, np.float32); prev = 0
            for k, b in enumerate(raw):
                sb = b - 256 if b > 127 else b
                sb = (prev + sb + 128) % 256 - 128
                s.data[k] = sb / 128.0
                prev = sb
            s.vol = vol / 64.0
            s.loop = (stype & 3) == 1
            s.lstart, s.llen = lstart, llen
            s.rel, s.ft = rel - 256 if rel > 127 else rel, ft - 256 if ft > 127 else ft
            s.name = nm
            samples.append(s)
    return dict(name=name, songlen=songlen, nch=nch, npat=npat, nins=nins,
                flags=flags, speed=speed, bpm=bpm, orders=orders,
                patterns=patterns, samples=samples)

def note_rate(note, s):
    return 8363.0 * 2.0 ** ((note - 49 + s.rel + s.ft / 128.0) / 12.0)

def xm_render(path, out_wav, seconds=None, sr=44100):
    if np is None:
        raise RuntimeError('numpy is required for XM playback')
    m = xm_load(path)
    state = [dict(sample=None, note=0, period=0.0, position=0.0, volume=0.0, memory={}) for _ in range(m['nch'])]
    speed, bpm = m['speed'], m['bpm']
    order = row = 0
    elapsed = 0.0
    written = 0
    chunks = []
    visited = set()
    limit = seconds if seconds is not None else float('inf')
    while order < len(m['orders']) and elapsed < limit:
        location = (order, row)
        if location in visited:
            break
        visited.add(location)
        pattern = m['patterns'][m['orders'][order]]
        jump = break_row = None
        for ci, (note, ins, vol, fx, param) in enumerate(pattern[row]):
            ch = state[ci]
            if ins:
                ch['sample'] = m['samples'][ins - 1]
                ch['volume'] = ch['sample'].vol
            if note == 97:
                ch['volume'] = 0.0
            elif note:
                ch['note'] = note
                ch['position'] = 0.0
                if ch['sample'] is not None:
                    ch['period'] = 7680.0 - (note - 1 + ch['sample'].rel) * 64 - ch['sample'].ft / 2.0
            if 0x10 <= vol <= 0x50:
                ch['volume'] = (vol - 0x10) / 64.0
            if fx == 0x0F:
                if param >= 32:
                    bpm = param
                elif param:
                    speed = param
                else:
                    return _write_xm_audio(m, chunks, out_wav, sr)
            elif fx == 0x0B:
                jump = param
            elif fx == 0x0D:
                break_row = (param >> 4) * 10 + (param & 15)
            elif fx == 0x0C:
                ch['volume'] = min(64, param) / 64.0
            elif fx in (1, 2, 0x21) or (fx == 0x0E and param >> 4 in (1, 2)):
                key = (fx, param >> 4) if fx in (0x0E, 0x21) else (fx, 0)
                amount = (param & 15) if fx in (0x0E, 0x21) else param
                if amount:
                    ch['memory'][key] = amount
                else:
                    amount = ch['memory'].get(key, 0)
                direction = param >> 4 if fx in (0x0E, 0x21) else fx
                units = amount * (4 if fx == 0x0E else 1)
                if fx in (1, 2):
                    units = amount * 4 * max(0, speed - 1)
                ch['period'] += -units if direction == 1 else units
        duration = min(speed * int(sr * 2.5 / bpm) / sr, limit - elapsed)
        end = int(round((elapsed + duration) * sr))
        length = end - written
        mix = np.zeros(length, np.float64)
        for ch in state:
            sample = ch['sample']
            if sample is None or not ch['note'] or not len(sample.data):
                continue
            rate = 8363.0 * 2 ** ((4608.0 - ch['period']) / 768.0)
            positions = ch['position'] + np.arange(length) * rate / sr
            left = np.floor(positions).astype(np.int64)
            fraction = positions - left
            right = left + 1
            if sample.loop and sample.llen:
                left = np.where(left >= sample.lstart + sample.llen,
                                sample.lstart + (left - sample.lstart) % sample.llen, left)
                right = np.where(right >= sample.lstart + sample.llen,
                                 sample.lstart + (right - sample.lstart) % sample.llen, right)
            valid_left = (left >= 0) & (left < len(sample.data))
            valid_right = (right >= 0) & (right < len(sample.data))
            first = sample.data[np.clip(left, 0, len(sample.data) - 1)] * valid_left
            second = sample.data[np.clip(right, 0, len(sample.data) - 1)] * valid_right
            mix += (first * (1 - fraction) + second * fraction) * sample.vol * ch['volume'] * 0.4
            ch['position'] += length * rate / sr
        chunks.append(mix)
        elapsed += duration
        written = end
        if jump is not None or break_row is not None:
            order = jump if jump is not None else order + 1
            row = break_row or 0
        else:
            row += 1
            if row >= len(pattern):
                order += 1
                row = 0
    return _write_xm_audio(m, chunks, out_wav, sr)


def _write_xm_audio(module, chunks, path, sr):
    mix = np.concatenate(chunks) if chunks else np.zeros(0)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(mix, -1, 1) * 32767).astype('<i2').tobytes())
    return module, len(mix) / sr

# ==========================================================================
# DATA RENDERER
# ==========================================================================
def render_song(path, idx, out_wav, sr=48000, tail=0.0):
    import tempfile
    h = Hachiemon(path)
    with tempfile.TemporaryDirectory(prefix='hachiemon-') as temporary:
        source = os.path.join(temporary, 'song.xm')
        info = build(h, idx, source)
        _, seconds = xm_render(source, out_wav, sr=sr)
    return dict(song=idx, ticks=info['rows'], seconds=seconds,
                channels=info['mapping'], renderer='XM preview; not hardware capture')

# ==========================================================================
# HARDWARE PIPELINE
# ==========================================================================

# ============================================================================
# 6. HARDWARE CAPTURE PIPELINE (mGBA)
# ============================================================================
TICK_HZ   = 59.7275
CAP_SR    = 44100          # the SDL disk writer always records 44.1 kHz stereo

ISO_PATCHES = [            # make the ROM play one song in isolation (see wiki)
    ('u16', 0x080002AC, 0x2002),   # boot: movs r0,#0 -> movs r0,#2 (start_song)
    ('u16', 0x0802211C, 0x4770),   # game music wrapper 1 -> bx lr
    ('u16', 0x08022150, 0x4770),   # game music wrapper 2 -> bx lr
]
SONG_ENTRY2 = 0x080691B8   # song table entry used by the title sequence

def make_iso_rom(h, idx, out):
    """Write a patched ROM that boots straight into song idx and plays nothing
    else (no jingles, no title-sequence fade, no attract-mode music)."""
    d = bytearray(open(h.path, 'rb').read())
    hdr = h.songs()[idx][1]
    struct.pack_into('<I', d, SONG_ENTRY2 - 0x08000000, hdr)
    for kind, addr, val in ISO_PATCHES:
        fmt = '<H' if kind == 'u16' else '<I'
        size = 2 if kind == 'u16' else 4
        struct.pack_into(fmt, d, addr - 0x08000000, val)
    open(out, 'wb').write(d)
    return out

def cap_one(romfile, secs, out_raw, mgba='mgba'):
    """Run one ROM head-less until `secs` of audio exist, then stop it.
    A ROM-specific battery save changes the boot timeline."""
    save = os.path.splitext(romfile)[0] + '.sav'
    if os.path.exists(save):
        os.remove(save)
    env = dict(os.environ, SDL_VIDEODRIVER='dummy', SDL_AUDIODRIVER='disk',
               SDL_DISKAUDIOFILE=os.path.abspath(out_raw))
    with open(out_raw, 'wb'):
        pass
    p = subprocess.Popen([mgba, '-C', 'audioSampleRate=32768', romfile], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = int(secs * CAP_SR * 4)          # stereo 16-bit frames
    t0 = time.time()
    while time.time() - t0 < secs * 2 + 25:
        if p.poll() is not None:
            break
        try:
            if os.path.getsize(out_raw) >= target:
                break
        except OSError:
            pass
        time.sleep(0.25)
    p.send_signal(signal.SIGINT)
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
    return os.path.getsize(out_raw) / (CAP_SR * 4)

def capture(h, songs, capdir='capture', workers=2, verbose=True):
    """Capture each song with the isolation ROM.  Two mGBA instances run in
    parallel (the sandbox has two cores).  Returns {song: file}."""
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(capdir, exist_ok=True)
    plan = {i: h.song(i)['ticks'] for i in songs}
    def one(i):
        rom = 'iso_s%02d.gba' % i
        make_iso_rom(h, i, rom)
        secs = math.ceil(plan[i] / TICK_HZ) + 14
        out = os.path.join(capdir, 's%02d.raw' % i)
        got = cap_one(rom, secs, out)
        return 'song %2d: %.1f s captured (%s)' % (i, got, out)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for msg in ex.map(one, songs):
            if verbose:
                print(msg, flush=True)
    return {i: os.path.join(capdir, 's%02d.raw' % i) for i in songs}

def cut_load(path):
    d = np.fromfile(path, dtype='<i2')
    n = len(d) // 2
    return d[:2 * n].reshape(-1, 2)[:, 0].astype(np.float32)   # mGBA writes L == R

def first_audible_tick(song):
    best = None
    for cd in song['channels']:
        for e in cd['events']:
            if e[1] in ('note', 'drum', 'pcm'):
                t = e[0]
                best = t if best is None else min(best, t)
                break
    return best if best is not None else 0

def cut_wavs(h, songs, capdir='capture', outdir='renders_hw'):
    """Cut each capture at the song's tick 0 and write exactly one pass.
    anchor = first audible sample - first audible tick (the captured ROM starts
    the song at boot, cycle 565892, so the first note is the only reference)."""
    os.makedirs(outdir, exist_ok=True)
    manifest = []
    for idx in songs:
        raw = os.path.join(capdir, 's%02d.raw' % idx)
        if not os.path.exists(raw):
            print('song %2d: no capture' % idx); continue
        x = cut_load(raw)
        s = h.song(idx)
        t0 = first_audible_tick(s)
        nz = int(np.argmax(np.abs(x[20:]) > 2)) + 20
        anchor = max(0, nz - int(round(t0 * CAP_SR / TICK_HZ)))
        ticks = s['ticks']
        n = int(round(ticks * CAP_SR / TICK_HZ))
        seg = x[anchor:anchor + n]
        expect = anchor + int(round(t0 * CAP_SR / TICK_HZ))
        rec = dict(song=idx, wav='song%02d.wav' % idx, ticks=ticks,
                   seconds=round(len(seg) / CAP_SR, 3), anchor_sample=anchor,
                   first_note_check_ms=round((nz - expect) / CAP_SR * 1000, 2),
                   peak=int(abs(seg).max()) if len(seg) else 0)
        with wave.open(os.path.join(outdir, rec['wav']), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(CAP_SR)
            w.writeframes(seg.astype('<i2').tobytes())
        manifest.append(rec)
        print('song %2d -> %s  %.2f s  first-note check %+.2f ms' %
              (idx, rec['wav'], rec['seconds'], rec['first_note_check_ms']))
    json.dump(manifest, open(os.path.join(outdir, 'manifest.json'), 'w'), indent=1)
    return manifest

def detect_onsets(x, sr, hop=0.002):
    hh = int(hop * sr)
    n = len(x) // hh
    e = np.log10(np.sqrt((x[:n * hh].reshape(n, hh) ** 2).mean(axis=1) + 1e-9))
    d = np.diff(e)
    thr = np.percentile(d, 99.5)
    idx = np.where((d > max(thr, 0.02)) & (e[1:] > np.percentile(e, 60)))[0]
    out = []
    for i in idx:
        t = (i + 1) * hop
        if not out or t - out[-1] > 0.05:
            out.append(t)
    return np.array(out)

def validate_drums(h, songs, rendersdir='renders_hw'):
    """Every parsed drum hit (PCM channels) must show up as an energy onset in
    the rendered audio.  Drums 2/4/14 point at an 8-byte silent stub."""
    rows = []; tot = hit = 0
    for idx in songs:
        wav = os.path.join(rendersdir, 'song%02d.wav' % idx)
        if not os.path.exists(wav):
            continue
        w = wave.open(wav)
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32)
        sr = w.getframerate()
        s = h.song(idx)
        pred = []
        for cd in s['channels']:
            if cd['slot'] not in (8, 9):
                continue
            for e in cd['events']:
                if e[1] in ('drum', 'pcm'):
                    d = e[3] if e[1] == 'drum' else None
                    if d not in (2, 4, 14):
                        pred.append(e[0] / TICK_HZ)
        m = 0; errs = []
        w2 = int(0.020 * sr)
        for t in pred:
            i = int(t * sr)
            if i < w2 or i + w2 >= len(x):
                continue
            before = float((x[i - w2:i] ** 2).mean()) + 1.0
            after = float((x[i:i + w2] ** 2).mean()) + 1.0
            if after / before > 1.25:
                m += 1
        tot += len(pred); hit += m
        rows.append(dict(song=idx, predicted=len(pred), matched=m))
        print('song %2d: %3d drum hits predicted, %3d matched' % (idx, len(pred), m))
    print('TOTAL: %d / %d drum hits confirmed' % (hit, tot))
    json.dump(rows, open(os.path.join(rendersdir, 'validation.json'), 'w'), indent=1)
    return rows

# ============================================================================
# 7. COMMAND LINE
# ============================================================================
def cmd_info(argv):
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    print('ROM: %s  (%d bytes)' % (h.path, os.path.getsize(h.path)))
    for i, (idx, hdr) in enumerate(h.songs()):
        s = h.song(idx)
        print('song %2d  hdr 0x%08X  mask 0x%03X  %2d ch  %4d ticks  %5.1f s' %
              (idx, hdr, s['mask'], len(s['channels']), s['ticks'], s['ticks'] / TICK_HZ))
    return 0

def cmd_dump(argv):
    out = 'dump/monsters_songs.json'
    if '-o' in argv:
        i = argv.index('-o'); out = argv[i + 1]; argv = argv[:i] + argv[i + 2:]
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    all_songs = [h.song(i) for i, _ in h.songs()]
    json.dump(all_songs, open(out, 'w'), indent=1)
    print('wrote %s (%d songs)' % (out, len(all_songs)))
    return 0

def cmd_samples(argv):
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    extract_samples(h, argv[1] if len(argv) > 1 else 'samples')
    return 0

def cmd_xm(argv):
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    outdir = argv[1] if len(argv) > 1 else 'EXPORTS'
    os.makedirs(outdir, exist_ok=True)
    which = [int(a) for a in argv[2:]] or [i for i, _ in h.songs() if i >= 2]
    for i in which:
        build(h, i, os.path.join(outdir, 'song%02d.xm' % i), write_mapping=False)
    return 0

def cmd_render_xm(argv):
    m, secs = xm_render(argv[0], argv[1],
                        seconds=float(argv[2]) if len(argv) > 2 else None)
    print('%s -> %s (%.1f s)' % (argv[0], argv[1], secs))
    return 0

def cmd_render(argv):
    rom = argv[0] if len(argv) > 2 else 'monsters_ue_rus.gba'
    song = int(argv[-2]); out = argv[-1]
    print(render_song(rom, song, out))
    return 0

def cmd_capture(argv):
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    outdir = argv[1] if len(argv) > 1 else 'capture'
    which = [int(a) for a in argv[2:]] or [i for i, _ in h.songs() if i >= 2]
    capture(h, which, capdir=outdir)
    return 0

def cmd_cut(argv):
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    capdir = argv[1] if len(argv) > 1 else 'capture'
    outdir = argv[2] if len(argv) > 2 else 'renders_hw'
    which = [int(a) for a in argv[3:]] or [i for i, _ in h.songs() if i >= 2]
    cut_wavs(h, which, capdir=capdir, outdir=outdir)
    return 0

def cmd_validate(argv):
    h = Hachiemon(argv[0] if argv else 'monsters_ue_rus.gba')
    outdir = argv[1] if len(argv) > 1 else 'renders_hw'
    which = [int(a) for a in argv[2:]] or [i for i, _ in h.songs() if i >= 2]
    validate_drums(h, which, rendersdir=outdir)
    return 0

def cmd_all(argv):
    rom  = argv[0] if argv else 'monsters_ue_rus.gba'
    base = argv[1] if len(argv) > 1 else '.'
    h = Hachiemon(rom)
    cmd_dump([rom, '-o', os.path.join(base, 'dump/monsters_songs.json')])
    extract_samples(h, os.path.join(base, 'samples'))
    os.makedirs(os.path.join(base, 'tracker'), exist_ok=True)
    for i, _ in h.songs():
        if i >= 2:
            build(h, i, os.path.join(base, 'tracker/song%02d.xm' % i))
    if shutil.which('mgba'):
        caps = capture(h, [i for i, _ in h.songs() if i >= 2],
                       capdir=os.path.join(base, 'capture'))
        cut_wavs(h, list(caps), capdir=os.path.join(base, 'capture'),
                 outdir=os.path.join(base, 'renders_hw'))
        if np is not None:
            validate_drums(h, list(caps), rendersdir=os.path.join(base, 'renders_hw'))
    else:
        print('mGBA not found: skipping hardware capture (everything else is done)')
    return 0

COMMANDS = dict(info=cmd_info, dump=cmd_dump, samples=cmd_samples, xm=cmd_xm,
                **{'render-xm': cmd_render_xm}, render=cmd_render,
                capture=cmd_capture, cut=cmd_cut, validate=cmd_validate, all=cmd_all)

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    cmd = argv.pop(0)
    if cmd not in COMMANDS:
        print('unknown command %r (try --help)' % cmd)
        return 2
    return COMMANDS[cmd](argv)

if __name__ == '__main__':
    sys.exit(main())
