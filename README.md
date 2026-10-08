# Hachiemon music format and sound-driver reference

This reference describes Natsume's Hachiemon sound driver in **Monsters, Inc. for Game Boy Advance**: sequence commands, channel state, instruments, envelopes, modulation, percussion and hardware playback.

Addresses and inventories refer to the examined 4 MiB image, whose header identifies `MONSTERS,INC` / `AMXE78` and whose MD5 is **`2bfd80868014971192e161837efee88f`**. The checksum identifies the examined image; the header alone does not establish that another image has identical contents. Other builds require separate verification.

Sequence state, hardware registers and final audio are separate layers. A stored pitch can change without a register write; a rest can leave echo audible; PCM can continue after its sequence enters a rest. Reproduction requires the state transitions and write conditions below, in addition to notes and sample bytes.

The companion [C reconstruction](gba/hachiemon.c) expresses the channel records and driver state machines directly; [its header](gba/hachiemon.h) defines the fixed-width layouts and public entry points. The reconstruction targets a 32-bit GBA ABI and keeps the examined ROM tables as fixed addresses where they are data-specific.

Stored multi-byte values are little-endian unless a note explicitly assembles its bytes in the opposite order. ROM addresses start at `08000000`; subtract that base for file offsets. Routine addresses identify Thumb instructions without the low-bit calling tag. Indices start at zero.

Contents: [driver](#1-driver-and-allocation) · [structures](#2-data-layout) · [commands](#3-sequence-language) · [updates](#4-frame-order-and-pending-writes) · [envelopes](#5-volume-and-envelope-state) · [pitch](#6-pitch-and-modulation) · [echo](#7-delayed-note-echo) · [PSG](#8-pulse-wave-and-noise) · [PCM](#9-directsound-pcm-playback) · [timing](#10-timing-and-recurrence) · [songs](#11-song-inventory) · [instruments](#12-complete-instrument-inventory) · [evidence](#13-evidence-and-verification-boundaries) · [references](#14-related-analysis-and-hardware-references).

## 1. Driver and allocation

### 1.1 Entry points

| Address | Purpose |
| --- | --- |
| `0803754C` | Reset context and assign fixed slot types |
| `08038258` | Advance sequences and update selected hardware voices |
| `080394E8` | Advance PCM pending-age counters |
| `08038434` | Start song by song-table index |
| `0803851C` | Set song table |
| `08038530` | Return context pointer |
| `0803853C` | Initialize channel with sequence address |
| `0803869C` | Stop channel and restore underlying allocation where applicable |
| `08039070` | Set sound-effect table |
| `08039084` | Start effect with per-slot priority comparison |
| `080392B0` / `080392C4` | Set PCM tables 1 / 2 |
| `0803967C` | Set percussion table |
| `08039690` | Mute music for nonzero argument; restore for zero |
| `080381A0` | Advance one channel |
| `08037F1C` | Fetch and dispatch a sequence byte |
| `08037AE0` | Interpret note according to mode and engine |
| `080388F0` | Read PSG frequency table |
| `08038E14` | Apply selected PCM channel to FIFO voice |
| `0803939C` / `080393F0` | Start PCM from semitone pitch / direct Q12 multiplier |
| `0803953C` | Handle PCM DMA block, age, ending/loop and pending rate changes |
| `08039508` / `08039520` | DMA completion handlers for the two voices |
| `08039AA0` | Indirect-call helper, `bx r0` |

The pointer slot at `03006E14` contains context `03001D90`. Game wrappers are `0802211C` for music and `08022150` for effects. Initialization near `08000288` supplies these pointers:

| ROM literal | Pointer | Data |
| --- | --- | --- |
| `08000330` | `080691B0` | Song table |
| `08000334` | `0806948C` | Effect table |
| `08000338` | `0806A1E0` | PCM table 1 |
| `0800033C` | `0806A2E0` | PCM table 2 |
| `08000340` | `0806A338` | Percussion table |

### 1.2 Logical slots and physical generators

Reset initializes **13 records**, each `0x64` bytes. Header masks address slots 0–11; slot 12 is auxiliary noise outside the mask. Six physical generators exist.

| Slot | Engine | Generator | Usual allocation |
| --- | --- | --- | --- |
| 0 / 4 | 0 | Pulse 1 | Music / overriding effect |
| 1 / 5 | 1 | Pulse 2 | Music / overriding effect |
| 2 / 6 | 2 | Wave RAM | Music / overriding effect |
| 3 / 7 | 3 | Noise | Music / overriding effect |
| 8 / 10 | 4 | FIFO A | Music / overriding effect |
| 9 / 11 | 5 | FIFO B | Music / overriding effect |
| 12 | 3 | Noise | Auxiliary record |

Slots 4–7 override corresponding music PSG slots while their sequence cursor is nonzero. Slots 10/11 similarly override 8/9. Overridden sequences can continue advancing; logical activity does not imply hardware ownership. Effect termination can restore underlying music registers.

Noise selection at `08038356` first prefers active slot 7. Otherwise, while music is enabled, it selects slot 12 only when slot 12 is active, slot 9 is active in mode 2, and slot 11 is active. It otherwise selects slot 3. Song start initializes slot 12 when mask bit `0x0004` is set, using context `+0x5B4`, the effect-table pointer in this image. These unusual instructions are established; their intended role and all effect interactions remain unresolved.

Music muting gates music advancement and allocation. Effect slots remain separately allocated. This differs from zero output gain applied to a continuously advancing song.

### 1.3 Finding another build

Find the note-table reader that doubles an index, adds a literal table address and loads a halfword. This image's 72 entries run from `0x002E` to `0x07DF`. Trace callers to the note handler and 32-destination dispatcher for `E0`–`FF`.

The game supplies five main data pointers through setter calls; they need not be fixed literals inside the driver. Locate initialization callers and validate each pointed-to structure. Percussion count 16 and base `0x23` characterize this image, not every version. Record stride, allocation, counts, addresses and behavior must be checked in another build.

## 2. Data layout

### 2.1 Song and effect headers

The song table at `080691B0` contains **20 u32 header pointers**. Headers begin at `08069200`. Entries 0/1 contain immediate-stop streams; 2–19 contain music.

| Header offset | Type | Meaning |
| --- | --- | --- |
| `+0x00` | u16 | Mask: bit 11 selects slot 0, down to bit 0 selecting slot 11 |
| `+0x02` | u8 | Allocation priority copied to channel `+0x61` |
| `+0x03` | u8 | Padding, zero here |
| `+0x04` onward | u32 per enabled bit | Stream pointers in ascending slot order |

Size is `4 + 4 × popcount(mask)`. Mask `0x0F0C` selects 0,1,2,3,8,9; all eighteen music entries use it. Song start stops those six music slots and auxiliary 12, then initializes enabled slots. Entry 0 addresses all twelve mask slots. Entries 0/1 use the `FF` stub at `08069448`.

The **effect table contains 107 u32 pointers**, `0806948C`–`08069637`, IDs 0–106. Each points to the same header structure, rather than paired stream pointers. Headers begin at `08069638`; immediate-stop stream is `080699AC`.

For each enabled effect slot, resident priority strictly greater than new priority blocks replacement; equal or lower permits it. A multi-slot effect can be partly accepted. Priority is not amplitude.

| ID | Header | Mask | Priority | Streams |
| --- | --- | --- | --- | --- |
| 0 | `08069638` | `0x091` | `0xFF` | 4,7,11: `080699AC` |
| 1 | `08069648` | `0x080` | `0x90` | 4: `080699AD` |
| 27 | `0806971C` | `0x001` | `0x48` | 11: `08069B8B` |
| 28 | `08069724` | `0x001` | `0x88` | 11: `08069B91` |
| 90 | `08069920` | `0x080` | `0x50` | 4: `0806A0B4` |
| 91 | `08069928` | `0x002` | `0x90` | 10: `0806A0DE` |
| 92 | `08069930` | `0x002` | `0x90` | 10: `0806A0E4` |
| 93 | `08069938` | `0x001` | `0x90` | 11: `0806A0EA` |
| 94 | `08069940` | `0x001` | `0x90` | 11: `0806A0F0` |
| 106 | `080699A4` | `0x010` | `0x58` | 7: `0806A1CD` |

IDs 91–94 are individual PCM fragments. Their IDs alone do not establish a complete jingle or musical identity.

### 2.2 Context fields

| Offset | Contents |
| --- | --- |
| `+0x000`–`+0x513` | 13 channel records; slot `n` at `0x64 × n` |
| `+0x514 + 0x34 × n`, `n=0,1,2` | Three 48-byte note rings; cursor at ring `+0x30` |
| `+0x5B0` / `+0x5B4` | Song / effect table pointers |
| `+0x5B8` / `+0x5DC` | Two embedded `0x24`-byte PCM structures |
| `+0x600` / `+0x604` | PCM tables 1 / 2 |
| `+0x608` | Percussion table |
| `+0x60C` | Music-state flags; bit 1 gates music |
| `+0x60F` | Update/start exclusion flag |
| `+0x610` | Selected song ID |

### 2.3 Channel record

Offsets are relative to one `0x64`-byte record. Unlisted fields should not be assigned speculative meanings.

| Offset | Type | Meaning |
| --- | --- | --- |
| `+0x04` | pointer | Echo history or zero |
| `+0x0C` | pointer | Main cursor; zero means stopped |
| `+0x10` / `+0x14` | pointer | Saved loop positions |
| `+0x18` | pointer | Single return position |
| `+0x1C` | pointer | Noise macro cursor |
| `+0x20` | u32 | Sequence wait |
| `+0x24` / `+0x28` | u32 | Base duration / macro wait |
| `+0x2C` / `+0x30` | s32 | Modulation delta / accumulator |
| `+0x34` | u32 | External gain-ramp counter |
| `+0x38` / `+0x3A` | u16 | Current / target Q12 gain |
| `+0x3C` | s16 | External gain-ramp step |
| `+0x3E` | u16 | Base PSG frequency |
| `+0x40` | u16 | PSG index, PCM pitch/multiplier or drum index; off sentinel `0x00FF` |
| `+0x42` | u16 | Quantized gain × live-volume product |
| `+0x44` / `+0x45` | u8 | Pending writes / fixed slot |
| `+0x46` / `+0x47` | u8 | Loop counts |
| `+0x48` | u8 | Pulse-1 hardware sweep |
| `+0x49` | u8 | Unsigned PSG register offset |
| `+0x4A` | u8 | Modulation initial delay |
| `+0x4B` / `+0x4C` | u8 | Modulation period / depth |
| `+0x4D` / `+0x4E` | u8 | Running delay / direction counter |
| `+0x4F`–`+0x52` | four u8 | Attack, decay, release threshold, release |
| `+0x53` / `+0x54` / `+0x55` | u8 | Envelope counter / step or remaining decrements / state |
| `+0x56` / `+0x57` / `+0x58` | u8 | Envelope enable / mode / engine |
| `+0x59` / `+0x5A` | u8 | Saved / live volume |
| `+0x5B` | u8 | Duty, wave selection or PCM-1 index |
| `+0x5D` | u8 | Register parameter; noise uses NR43 |
| `+0x5E` | s8 | Semitone transpose |
| `+0x5F` / `+0x60` | u8 | Echo delay / cached delayed note |
| `+0x61` / `+0x62` / `+0x63` | u8 | Priority / transient duration high byte / routing |

## 3. Sequence language

Zero-time commands run together until a nonzero wait or stop. Operand counts exclude the opcode; note operands depend on mode.

| Opcode | Operands | Behavior |
| --- | ---: | --- |
| `00`–`7F` | Mode-dependent | Note |
| `80`–`8F`, `E0` | 1 | Rest, following duration byte plus one |
| `90`–`CF` | 0 | Ignored |
| `D0`–`DF` | 0 | Low nibble sets saved/live volume; always marks volume dirty |
| `E1` | 1 | Base duration, no added one |
| `E2` | 1 | Register parameter; flags `0x11` |
| `E3` | 1 | Instrument field; wave-selection flag `0x20` |
| `E4`–`E7` | 0 | Instrument = `00`/`40`/`80`/`C0`; duty flag `0x04` |
| `E8` | 1 | Signed transpose |
| `E9` | 1 | Echo delay, maximum `0x30` |
| `EA` | 1 | Routing; flag `0x40` |
| `EB`–`EF` | Note-dependent | Note handler with opcode as note; unused in music |
| `F0` | 1 | Loop-1 count and position after operand |
| `F1` | 0 | Decrement modulo 256; jump if result nonzero |
| `F2` / `F3` | 1 / 0 | Same for loop 2 |
| `F4` | 4 | Absolute jump |
| `F5` | 4 | Save return after operands, then jump |
| `F6` | 0 | Restore return position |
| `F7` | 1 | Sweep; zero becomes `0x08`; flag `0x08` |
| `F8` | 1 | Unsigned PSG offset; no immediate pitch-dirty flag |
| `F9` | 2 | Modulation delay and packed depth/period |
| `FA` | 1 | Transient duration high byte |
| `FB` | 4 | Envelope parameters, enable and eligible re-arm |
| `FC` / `FD` | 0 | Enable / disable; both re-arm eligible engines |
| `FE` | 1 | Mode |
| `FF` | 0 | Slot-specific stop |

Loop count zero wraps to 255 on its first end and gives 256 ordinary passes. Reusing a loop record inside itself overwrites its position/count. Calls have one return field, not a stack. Backward jumps continue with existing state.

### 3.1 Modes

| Mode | Pulse/wave, engines 0–2 | Noise, engine 3 | PCM, engines 4–5 |
| --- | --- | --- | --- |
| 0 | Note + duration; table pitch | Note + duration; percussion macro | Note + duration; PCM 1, semitone pitch |
| 1 | Two bytes form raw frequency `(first << 8) \| second`; wait = `E1` | Wait = `E1`; no second pitch byte | Two bytes form direct Q12 multiplier; wait = `E1` |
| 2 | Loads duration without normal tonal start | Note + duration; percussion macro | Note + duration; PCM 2 percussion |

Modes 0/2 use `((FA_value << 8) | duration_byte) + 1`, up to 65,536 frames. `FA` is cleared at each update's beginning and after consumption; it must precede its note in the same command run. It does not extend rests.

Mode-1 PSG stores index zero, resets echo cache and sets flags `0x13`; it bypasses ordinary modulation/envelope note setup. Mode-1 PCM passes its halfword to `080393F0` as a Q12 multiplier, not a semitone number. Zero base duration permits further fetching that frame. Invalid modes have no useful ordinary note-start interpretation.

Percussion index is `clamp(note − table_base, 0, table_count − 1)`. PCM mode 2 selects table 2 and plays at `table_base + index`. PCM mode 0 applies signed transpose; mode 2/direct multiplier do not.

### 3.2 Notes and rests

Normal notes restore saved volume. Pulse/wave mode 0 stores index/base frequency, resets echo cache, sets `0x13`, initializes modulation and calls envelope setup even when its clock is disabled.

Rest stores **`0x00FF`**, clears base frequency/live volume, resets echo cache, loads wait and sets **volume flag `0x02`**. Saved volume, envelope, modulation and macro cursor survive. Continued effects can change its audible result. A later note restores saved level.

PCM rests do not start or stop FIFO playback. `FF` clears sequence activity and invokes slot-specific handling, potentially restoring another logical voice.

## 4. Frame order and pending writes

The game advances PCM pending age before sequences. The driver visits slots **12 down to 0**, subject to music gating and its exclusion flag. For each active slot:

1. Clear transient `FA` state.
2. Decrement a nonzero sequence wait.
3. Fetch until a wait or stop.
4. Pulse/wave: clock modulation, then enabled envelope. Noise: decrement macro wait and execute until a new wait/end. PCM: clock modulation.
5. Advance an active external gain ramp.

It then selects physical allocations and applies registers. Echo history is recorded afterward. FIFO consumption and DMA completion occur between musical frames.

Zero-delay modulation can apply its first step on a note's starting frame. Envelope state 1 can consume its delay and enter attack setup that same frame.

Initialization sets cursor/control positions and waits; flags `0x5F`, sweep `0x08`, saved volume 15, live volume zero, note `0x00FF`, echo cache `0xFF`, gain/target `0x1000`, routing 3. It clears modulation, mode, transpose, echo delay, register parameter and envelope enable. It does **not** blanket-clear the record. Engine/slot and echo pointer survive. Envelope parameters/state are not all cleared on reuse. Full reset sets the master-state byte to `1`, selected song ID to `2`, configures the sound master and PCM mixer, and loads wave bank 0 before playback.

| Flag | Pending action |
| --- | --- |
| `0x01` | Frequency/noise parameter |
| `0x02` | Volume/envelope register |
| `0x04` | Pulse duty |
| `0x08` | Pulse-1 sweep |
| `0x10` | Trigger/start |
| `0x20` | Wave selection/load |
| `0x40` | Routing |

Writers consume flags according to engine. Equal-volume commands still request writes. Pulse volume/duty writes request a trigger. Noise volume writes request its frequency/control writer too, triggering the generator. Repeated levels can be audibly significant.

Maintain hardware frequency/level separately from RAM. A pulse envelope retrigger without pitch flag writes the **base** frequency, excluding retained `F8`/modulation. With pitch dirty it writes the adjusted value. Wave attenuation does not share that pulse-trigger behavior.

## 5. Volume and envelope state

### 5.1 Gain and hardware levels

Common gain is Q12, initially `0x1000`. Writers store the low halfword of `(gain × live_volume) & 0xFFFFF000` at `+0x42`; its upper nibble supplies level. Storage width/truncation matter when gain changes.

External ramps are separate from ADSR. At `080379F8`, a nonzero signed step activates the ramp. Counter decreases each frame; while nonzero, gain increases by the signed step with halfword storage. Changed quantized level marks volume dirty. Completion assigns target gain, clears step and stops the channel. No command above directly configures these fields.

Pulse/noise use hardware initial-volume nibbles with automatic envelope pace zero. Software writes shape their amplitude. Wave uses five register levels. PCM uses 50%/100% rather than sixteen linear amplitudes.

### 5.2 Software ADSR

`FB A D S R` is discrete. **`S` is a remaining-duration threshold, not sustain amplitude.** Attack/decay begin at current live volume, initially restored from saved note level.

| Byte | High nibble | Low nibble |
| --- | --- | --- |
| `A` | Consecutive frame increments | Amount per increment |
| `D` | Consecutive frame decrements | Amount per decrement |
| `R` | Frames between decrement opportunities | Number of one-level decrements |

Counters/steps wrap at eight bits. Release gate opens when unsigned `S >= entire_u32_sequence_wait`. Setup of states 1–4 checks it; an open gate performs release setup and immediately clocks release once.

| State | Setup and clock |
| --- | --- |
| 0, ended | Live volume zero; no active step |
| 1, delay | Counter 1, step zero; decrement to zero enters attack setup |
| 2, attack | High nibble zero skips to decay. Otherwise load count/positive step. Decrement first: `0xFF` transitions without increment; otherwise add step, clamp 15 |
| 3, decay | High nibble zero skips to sustain. Otherwise load count/negative step. Decrement first: `0xFF` transitions without decrement; otherwise subtract step, clamp zero |
| 4, sustain | Poll gate. At setup, `S=0` enters release directly |
| 5, release | High nibble zero ends immediately. Otherwise counter 1, remaining decrements = low nibble. At counter zero, end if remaining byte is zero; otherwise decrement remaining byte, subtract one level with zero clamp, reload counter from high nibble |

Attack count `n` gives `n` increments on consecutive frames, followed by a transition frame; decay is analogous. Their high nibbles are not intervals. Release can decrement on its gate-opening frame. After the final decrement, ending occurs on the subsequent zero-step opportunity. Volume zero alone does not erase state.

Ordinary pulse/wave note setup occurs even with envelope enable zero; only dispatcher `08037860` is enable-gated. `FB`/`FC` enable and re-arm state 1 on engines 0–2. **`FD` disables and also re-arms**, preserving parameters. Setup can release/end through the gate. Rest leaves envelope state active.

Noise/PCM do not clock this envelope. On PCM, enable instead shortens life by the complete release byte, without an amplitude-release curve.

## 6. Pitch and modulation

### 6.1 PSG table

Normal pulse/wave uses `index = clamp(note + signed_transpose − 0x24, 0, 71)`, then `base_register = T[index]`. The 72 u16 entries at `08132108`, decimal, are:

```
46   159  263  365  460  547  634  712  787  857  925  988
1047 1103 1156 1206 1254 1298 1340 1380 1418 1453 1486 1518
1548 1576 1602 1627 1651 1673 1694 1714 1733 1750 1767 1783
1798 1812 1825 1837 1849 1860 1871 1881 1891 1899 1908 1916
1923 1930 1937 1943 1949 1955 1960 1965 1970 1974 1978 1982
1985 1989 1992 1995 1998 2001 2004 2006 2009 2011 2013 2015
```

Nominal pulse range is C2–B7; integer periods determine actual tuning. Pulse frequency is `131072 / (2048−register)` Hz. Wave point rate is `2097152 / (2048−register)` Hz; waveform repetitions determine tone frequency. Note `0x3F` selects index 27/register `0x065B`, about 311.3 Hz on pulse.

### 6.2 Offset and ping-pong modulation

`F8 x` is unsigned register offset, not cents/semitones, and does not force a write. Adjusted pitch is:

`clamp(signed16(base_register + F8 + (accumulator >> 4)), 0, 0x7FF)`.

Accumulator shift is arithmetic; signed-halfword conversion precedes clamping. A constant register offset represents different intervals at different notes.

`F9 delay packed` sets delay, depth `packed >> 4`, period `packed & 15`. Normal tonal setup loads delay, counter `period >> 1`, accumulator zero and:

`delta = signed_divide((T[index+2] − T[index]) × depth, period >> 1)`.

Difference/product follow 32-bit ARM arithmetic. Signed division truncates toward zero; this image's helper returns zero for divisor zero. At indices 70/71, setup reads beyond the table: the next halfwords are **2016** and **24576**. Clamping those reads changes edge behavior.

With nonzero depth, consume outstanding delay first. Then each frame adds delta with word wrap, marks pitch dirty, and updates direction counter. Nonzero counter decreases; zero reloads the full period and negates delta. Reversal follows addition; initial direction span differs from later spans. A sine LFO does not match this discrete ping-pong pattern.

`F9` changes parameters without immediately rerunning setup; existing counters/delta remain until setup. Depth zero gates the clock. Accumulated offset, flags and audible frequency remain separate.

### 6.3 PCM fixed-point pitch

Q12 table at `081321F6`:

```
4096 4339 4597 4870 5160 5467 5792 6137 6501 6888 7298 7732
```

For nonnegative semitones `s`, use `Q[s mod 12] << floor(s/12)`. For negative `s`, write `−s=12q+r`: if `r=0`, use `4096 >> q`; otherwise `Q[12−r] >> (q+1)`. Retain ARM word width and exceptional shift behavior. Ordinary low notes play below native rate; there is no six-octave lower clamp.

For native rate `N`, start computes `min(((N × Q12(pitch−base)) modulo 2^32) >> 12, 0x78000)`. Mode 1 substitutes its direct halfword multiplier. All scale bytes are zero. The nonzero-scale path references suspect address `08000E02` through literal `0803924C`; behavior is unestablished and should not receive an assumed fine-tuning formula.

PCM modulation uses the same clock, with delta from the difference between rates at raw pitch and raw pitch + 2, scaled by depth/divided by `period >> 1`. Setup uses raw pitch before transpose, while base playback rate includes transpose. Mode-2 PCM modulation is not exercised by the music evidence here.

Pending offset is `accumulator >> 4`. DMA completion applies `base_rate + offset` and recalculates timer without repeating the initial rate clamp. Integer quantization and commit timing affect audible pitch.

## 7. Delayed-note echo

`E9` delays **note history**, not sampled audio. Slots 0–2 have 48-byte rings, initially `0xFF`, cursor zero. Slots without a history pointer cannot use this path.

With live volume zero and delay nonzero, read `(cursor−delay) mod 48`, compare with cache `+0x60`. Unchanged byte causes **no echo register write**. Changed valid note writes `T[delayed_index]+F8`, signed-halfword converted/clamped, without current modulation. Echo level is ordinary gain applied to volume 3. Changed `0xFF` writes silence. Notes/rests reset cache to `0xFF`.

After hardware application, record current index if live volume is nonzero, otherwise `0xFF`, then advance modulo 48. Recording continues when delay is disabled. Delay 48 reads oldest entry before overwrite.

A skipped cached value preserves prior hardware state, including audible level. Raw volume zero therefore need not mean silence. Clearing/retriggering output every echo frame changes behavior. Echo bypasses the normal writer and has its own trigger conditions.

## 8. Pulse, wave and noise

### 8.1 Registers and pulse behavior

| Generator | GBA register addresses |
| --- | --- |
| Pulse 1 | `04000060` sweep; `04000062` duty/volume; `04000064` frequency/control |
| Pulse 2 | `04000068` duty/volume; `0400006C` frequency/control |
| Wave | `04000070` bank/enable; `04000072` volume; `04000074` frequency/control |
| Noise | `04000078` volume; `0400007C` polynomial/control |
| Common | `04000080` PSG master/routing; `04000082` PCM mix/routing; `04000084` enable |

Duty uses instrument bits 6–7: 12.5%,25%,50%,75%. `E4`–`E7` request duty writes; `E3` sets a different flag despite storing the same field, so an immediate duty change cannot be assumed.

`F7` supplies Pulse 1 NR10 sweep, not velocity. Low three bits select shift, bit 3 subtraction/addition, bits 4–6 pace. Its independent hardware clock and trigger/overflow rules differ from `F9`. Active sweep is absent from these songs; full interaction with modulation remains unverified. Related register behavior is described by [Pan Docs](https://gbdev.io/pandocs/Audio_Registers.html).

### 8.2 Wave RAM

Four patterns start at `0806944C`. The driver copies sixteen bytes into a writable bank and switches bank selection, using 32 points rather than combined 64-point mode. Decode **high nibble then low nibble, per byte in order**.

| Bank | Exact bytes | Points |
| --- | --- | --- |
| 0 | `FF 00 FF 00 FF 00 FF 00 FF 00 FF 00 FF 00 FF 00` | `[15,15,0,0]` eight times |
| 1 | `BF DC DC B9 64 32 32 04 BF DC DC B9 64 32 32 04` | Complex sixteen-point shape twice |
| 2 | `FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF` | Constant 15, DC |
| 3 | `01 23 45 67 89 AB CD EF 01 23 45 67 89 AB CD EF` | Ramp 0–15 twice |

Bank 1 starts `11,15,13,12,13,12,11,9,6,4,3,2,3,2,0,4`. Full-bank recurrence is `65536/(2048−register)` Hz. Bank 0 gives eight times it; banks 1/3 twice. Universal wave octave correction is incorrect.

Volume lookup at `0813219A`:

```
00 60 60 60 40 40 40 40 80 80 80 80 20 20 20 20
```

Mute at 0; 25% at 1–3; 50% at 4–7; forced 75% at 8–11; 100% at 12–15. The byte occupies `SOUND3CNT_H`'s high byte. Examined mGBA attenuation shifts four-bit points for 25%/50%, truncates `3×point/4` for 75%. Scaling a centered floating waveform does not reproduce those points.

### 8.3 Noise percussion

At `0806A338`: u16 count 16, u16 base 35, sixteen macro pointers. Noise modes 0/2 start a macro with zero wait. Its restricted executor supports:

| Byte | Action |
| --- | --- |
| `Dn` | Saved/live level; wait from `E1`; volume/frequency dirty |
| `E1 x` | Step duration `x`, no added one |
| `E2 x` | NR43 and pending frequency/trigger |
| `FF` | Zero saved/live volume; clear macro |
| Other | Consumed without supported macro action |

Parameter commands run until a volume step loads a wait. New percussion replaces old macro; main rest does not clear it. **Volume steps and NR43 changes both can retrigger noise**, even at equal levels. A drum is not one fixed onset noise color.

NR43 bits 0–2 are divisor `r`, bit 3 width, bits 4–7 shift `s`. Clock is `524288/r/2^(s+1)` Hz, `r=0.5` for encoded zero. Thus `0x10`,`0x11`,`0x12` mean **262144,131072,65536 Hz**. This is polynomial clock, not full-pattern repetition frequency.

Full periods are 32,767/127 steps. Exact recurrence requires convention, seed and phase. GBATEK's Galois form right-shifts and conditionally XORs `0x6000`/`0x60`, with seeds `0x4000`/`0x40`. An equivalent Fibonacci form feeds bottom-two-bit XOR to bit 14 and, for narrow mode, bit 6; seeds require conversion between conventions. Preserve recurrence/retriggers rather than a short slice. See [GBATEK noise](https://rust-console.github.io/gbatek-gbaonly/#gba-sound-channel-4---noise).

Noise macros and PCM drums are separate paths. Empty macro does not imply empty PCM at that index.

### 8.4 Routing and output

Ordinary `EA` selectors 0–3 mean disabled/right/left/both; default 3. PSG mask/data tables are `081321AA`/`081321AE`; PCM routing mask/data are `081321BE`/`081321C2`. Invalid selectors can overread rather than saturating. Routing applies after allocation.

Observed initialization uses `SOUNDCNT_L=0xFF77`, `SOUNDCNT_H=0x400E`, `SOUNDCNT_X=0x0080`, `SOUNDBIAS=0x0200`: normal 32,768 Hz output lattice and nine-bit PWM resolution. It seeds wave RAM with bank 0, then alternates the wave-bank select as patterns are loaded. CPU clock is 16,777,216 Hz. Oscillators/FIFOs advance between musical updates. Output samples held levels, combines routed voices, applies gain/bias, clips/quantizes and holds the result.

In the examined mixer model, full PSG spans 240 DAC units; 50% PCM spans 512, 100% PCM 1024. Input widths/scaling establish balance; per-voice normalization loses it. Integer mixing/scheduling can be examined in [mGBA GBA audio](https://github.com/mgba-emu/mgba/blob/0.10.2/src/gba/audio.c).

Preserve oscillator/FIFO holds. Linear interpolation smooths pulse edges/PCM holds and removes harmonics. Fast noise aliases against output sampling. Phase, priming, point quantization, shared clipping and sampling phase affect waveform. Analog filtering/host processing are further stages; register agreement alone does not establish identical recordings.

## 9. DirectSound PCM playback

### 9.1 Data

PCM 1 at `0806A1E0` contains 64 pointers, selected by raw `E3` for modes 0/1. PCM 2 at `0806A2E0` has sixteen, selected by percussion index. Out-of-range PCM-1 indices are not guaranteed a silent fallback.

| Blob offset | Type | Meaning |
| --- | --- | --- |
| `+0x00` | u32 | Payload length, bytes |
| `+0x04` | u16 | Native Hz |
| `+0x06` | u8 | Scale; zero here |
| `+0x07` | u8 | Base note |
| `+0x08` | u32 | Loop flags; bit 0 enables |
| `+0x0C` | u32 | Loop start relative to payload |
| `+0x10` onward | signed bytes | Eight-bit mono PCM |

Header is not audio; first payload bytes are not an extra envelope header. Preserve bytes/offsets exactly. Blobs are generally four-byte aligned. Native duration is length/native Hz; actual duration depends on pitch.

Eighty entries reference **72 unique blobs, 788,464 payload bytes, twelve looped blobs**. PCM 1 has 64 distinct addresses; PCM 2 nine, one shared with PCM 1. PCM-2 indices 2/4/14 share PCM-1 index 0's eight-byte silent stub. Indices 6/8/10/12/13/15 share a 6,400-byte blob. Aliases retain distinct sequence identities and playback pitches.

### 9.2 DMA voices

Slots 8/10 share FIFO A, 9/11 FIFO B, directly. Maximum PCM polyphony is two; no larger software PCM mix exists.

| PCM record offset | Type | Meaning |
| --- | --- | --- |
| `+0x00` | pointer | Sample header; zero means free |
| `+0x04` | u32 | Base rate |
| `+0x08` | u32 | DMA position accounting |
| `+0x0C` | u32 | Bit 0 inhibits ageing/block progress; bit 1 pending modulation |
| `+0x10` | u32 | Timer word |
| `+0x14` | s32 | Pending rate offset |
| `+0x18` | u16 | Life threshold |
| `+0x1A` / `+0x1C` | u16 | Committed age / pending frames |
| `+0x1E` | u8 | Voice 1/2 |
| `+0x1F` | u8 | Pitch, zero for direct multiplier |
| `+0x20` | u8 | Volume-table index |

DMA destinations are `040000A0`/`040000A4`, control `0xF6400004`, source `blob+0x10`. Start resets/primes 32 FIFO bytes; subsequent requests transfer sixteen. Timer 0 clocks A, timer 1 B. Position, pending age and rate changes commit at DMA completion.

For positive driver rate `R`, ordinary interval is `floor(16777216/R)`, reload `0x10000−interval`, enabled word formed with `0x00800000`. Actual rate is `16777216/interval`, subject to written timer width. Preserve integer division/reload behavior. Exceptional rates need actual arithmetic/timer semantics, not an invented minimum.

Mix masks are `081321CC`/`081321F2`, two sixteen-entry volume groups `081321D0`. Effective nonzero levels 1–7 select 50%, 8–15 select 100%. Zero live volume at pending start stops the old voice without replacement. Volume-only commands do not automatically rewrite a playing FIFO; application centers on start flag `0x10`.

### 9.3 Life, endings and loops

Without envelope enable, life starts as sequence wait. With enable, subtract the full release byte if wait exceeds it; otherwise one. Store in **u16** life field. `FB 00 00 00 01` shortens life one frame; it is not an amplitude release.

Each enabled frame increments pending age unless bit 0 inhibits it. DMA completion adds pending age to committed age, clears pending age and advances position sixteen bytes. Non-looping exhaustion occurs at accounted position `length−32`; age stop occurs when committed age reaches life. A new note can preempt either.

At looping exhaustion, DMA restarts from `blob+0x10+loop_start`, position resets there, life/age remain. Buffered FIFO/restart phase affect the boundary waveform. The eight-byte stub requires unsigned subtraction in the end test.

Rest does not stop DMA. Explicit song/effect control can stop or suspend according to allocation. Endings are takeover, exhaustion, committed age or explicit control.

In checked song 10, relevant age stops occur about one frame after the threshold, letting release-byte-one notes cover their intended duration. This is observed scheduling, not a universal extra frame for arbitrary rates/IRQ phases. Pending-age/DMA behavior is the general definition.

## 10. Timing and recurrence

One frame is **280,896 CPU cycles**, or **59.72750057 Hz / 16.7427063 ms** at 16,777,216 Hz. Music normally advances once per enabled frame. Durations count driver updates, not output samples.

Song 2 ends at 864 frames, about 14.466 s. Song 10's six streams recur every 2,880 frames, about 48.219 s. The examined full-game run holds musical progression twice, at source positions 765/2526. These add scheduling time rather than encoded rests; precise cause remains unresolved.

Loops are per stream. Repeated addresses alone do not establish complete recurrence: counters, returns, envelope/modulation, mode, instrument, history/cache, flags and phase also matter. Do not reset them at backward jumps.

Preserve lead-ins and compatible common event periods. Song 17's melodic period is 1,968 frames; FIFO-B percussion 1,152. Their least common multiple is **47,232 frames**, about **790.791 s**. Noise ends immediately. Song 19 has 24-frame lead-in and 768-frame period, first boundary 792. Event recurrence need not equal oscillator/analog waveform recurrence.

## 11. Song inventory

Pointers, masks and priorities come directly from the ROM. Frame columns describe event/sequence boundaries rather than complete hardware-state recurrence. Song 2 is the title-screen theme; other IDs retain numbers instead of unsupported names.

| ID | Header | Mask | Priority | Streams, slot:address | PCM 1 indices | PCM 2 indices | First boundary, frames | Lead-in | Joint event period |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |
| 0 | `08069200` | `0xFFF` | `0xF0` | 0:`08069448`; 1:`08069448`; 2:`08069448`; 3:`08069448`; 4:`08069448`; 5:`08069448`; 6:`08069448`; 7:`08069448`; 8:`08069448`; 9:`08069448`; 10:`08069448`; 11:`08069448` | — | — | 0 | — | — |
| 1 | `08069234` | `0xF0C` | `0xF0` | 0:`08069448`; 1:`08069448`; 2:`08069448`; 3:`08069448`; 8:`08069448`; 9:`08069448` | — | — | 0 | — | — |
| 2 | `08069250` | `0xF0C` | `0xC0` | 0:`080D2860`; 1:`080D28C3`; 2:`080D2926`; 3:`080D29A1`; 8:`080D2A4C`; 9:`080D2AE2` | 15, 19, 20 | — | 864 | — | — |
| 3 | `0806926C` | `0xF0C` | `0xA0` | 0:`0806D910`; 1:`0806D9D5`; 2:`0806DA9B`; 3:`0806DB37`; 8:`0806DC30`; 9:`0806DB38` | 17, 19 | 0, 5, 7, 11 | 976 | 208 | 768 |
| 4 | `08069288` | `0xF0C` | `0xA0` | 0:`080A8300`; 1:`080A837E`; 2:`080A83FD`; 3:`080A8464`; 8:`080A852D`; 9:`080A8465` | 18 | 1, 5, 7 | 896 | 0 | 896 |
| 5 | `080692A4` | `0xF0C` | `0xA0` | 0:`08117F00`; 1:`08117F60`; 2:`08117FBF`; 3:`08118072`; 8:`08118105`; 9:`08118073` | 17, 18 | 1, 5, 7, 11 | 768 | 512 | 256 |
| 6 | `080692C0` | `0xF0C` | `0xA0` | 0:`0806DD20`; 1:`0806DE4D`; 2:`0806DF7A`; 3:`0806E129`; 8:`0806E226`; 9:`0806E12A` | 16, 18 | 1, 5, 7 | 1536 | 384 | 1152 |
| 7 | `080692DC` | `0xF0C` | `0xA0` | 0:`0806E350`; 1:`0806E3CC`; 2:`0806E44C`; 3:`0806E4AF`; 8:`0806E548`; 9:`0806E4B0` | 16 | 0, 5, 7 | 768 | 256 | 512 |
| 8 | `080692F8` | `0xF0C` | `0xA0` | 0:`0810A9F0`; 1:`0810ABBD`; 2:`0810AD8A`; 3:`0810ADEC`; 8:`0810ADED`; 9:`0810AE3C` | 17, 63 | — | 1408 | 0 | 1408 |
| 9 | `08069314` | `0xF0C` | `0xA0` | 0:`0810F1A0`; 1:`0810F2F3`; 2:`0810F447`; 3:`0810F597`; 8:`0810F754`; 9:`0810F87D` | 15, 19, 53, 54 | — | 2688 | 448 | 2240 |
| 10 | `08069330` | `0xF0C` | `0xA0` | 0:`0806A3E0`; 1:`0806A591`; 2:`0806A6E3`; 3:`0806A896`; 8:`0806AB35`; 9:`0806AA05` | 17, 18, 62 | 0, 3, 12, 13, 15 | 2880 | 0 | 2880 |
| 11 | `0806934C` | `0xF0C` | `0xA0` | 0:`0806AD20`; 1:`0806AF58`; 2:`0806B190`; 3:`0806B35F`; 8:`0806B718`; 9:`0806B360` | 17, 19, 20 | 1, 5, 7 | 3136 | 448 | 2688 |
| 12 | `08069368` | `0xF0C` | `0xA0` | 0:`0806B8D0`; 1:`0806BAE1`; 2:`0806BCAF`; 3:`0806BE66`; 8:`0806C06F`; 9:`0806BE67` | 15, 17 | 1, 5, 7 | 3072 | 384 | 2688 |
| 13 | `08069384` | `0xF0C` | `0xA0` | 0:`0806C260`; 1:`0806C375`; 2:`0806C4EF`; 3:`0806C694`; 8:`0806C7C4`; 9:`0806C695` | 15 | 5, 7, 11 | 2496 | 192 | 2304 |
| 14 | `080693A0` | `0xF0C` | `0xA0` | 0:`0806C900`; 1:`0806CB80`; 2:`0806CDDB`; 3:`0806D02C`; 8:`0806D173`; 9:`0806D02D` | 16 | 0, 5 | 2240 | 0 | 2240 |
| 15 | `080693BC` | `0xF0C` | `0xA0` | 0:`0810AFD0`; 1:`0810B07E`; 2:`0810B134`; 3:`0810B1FA`; 8:`0810B2C3`; 9:`0810B1FB` | 15, 53 | 1, 5, 7 | 1152 | 0 | 1152 |
| 16 | `080693D8` | `0xF0C` | `0xA0` | 0:`080ADD50`; 1:`080ADFAE`; 2:`080AE1D5`; 3:`080AE34A`; 8:`080AE5B3`; 9:`080AE34B` | 15, 17 | 1, 5, 6, 7, 8, 10, 12 | 2560 | 0 | 2560 |
| 17 | `080693F4` | `0xF0C` | `0xA0` | 0:`080BEAF0`; 1:`080BECFF`; 2:`080BEEDC`; 3:`080BEFFF`; 8:`080BF0C8`; 9:`080BF000` | 15 | 3, 7 | 1968 | 0 | 47232 |
| 18 | `08069410` | `0xF0C` | `0xA0` | 0:`0806D3B0`; 1:`0806D52B`; 2:`0806D692`; 3:`0806D78D`; 8:`0806D856`; 9:`0806D78E` | 15, 16 | 3, 7 | 960 | 0 | 960 |
| 19 | `0806942C` | `0xF0C` | `0xA0` | 0:`081181B0`; 1:`0811822A`; 2:`081182A4`; 3:`08118325`; 8:`081183F0`; 9:`08118326` | 18, 19 | 0, 1, 5, 7 | 792 | 24 | 768 |

### 11.1 Song 10 reference case

Song 10 exercises all six voices, macros, multiple melodic samples, PCM drums, envelopes, pitch offsets and ping-pong modulation. Pulse duties are fixed **25%/50%**, wave **bank 1**. It does not contain changing pulse duty. A PWM-like sound can involve wave shape, recorded PCM components or output sampling; identification requires voice/time position.

PCM-1 indices are 17/18/62; PCM-2 0/3/12/13/15. The last three share data but different pitches. The checked first-cycle noise trace has **604 audible retriggers**. Ignoring equal-level writes, using drum ID as noise pitch or using a short polynomial slice changes it.

## 12. Complete instrument inventory

Addresses point to headers. Length/loop start count payload bytes; dash means no loop. Repeated addresses are aliases. All scale bytes are zero.

| Table | Index | Header address | Payload bytes | Native Hz | Base note | Loop start |
| --- | --- | --- | ---: | ---: | --- | ---: |
| 1 | 0 | `0806A320` | 8 | 44100 | `0x3C` | — |
| 1 | 1 | `08077CC0` | 10160 | 12000 | `0x3C` | — |
| 1 | 2 | `0807A480` | 9968 | 15000 | `0x3C` | — |
| 1 | 3 | `0807CB80` | 11116 | 12000 | `0x3C` | — |
| 1 | 4 | `0807F700` | 3000 | 10000 | `0x3C` | — |
| 1 | 5 | `080802D0` | 7260 | 10000 | `0x3C` | — |
| 1 | 6 | `08081F40` | 1300 | 10000 | `0x3D` | — |
| 1 | 7 | `08082470` | 15460 | 10000 | `0x3C` | — |
| 1 | 8 | `0811E140` | 5392 | 16000 | `0x3C` | — |
| 1 | 9 | `080860F0` | 4000 | 10000 | `0x3C` | — |
| 1 | 10 | `080870A0` | 13700 | 10000 | `0x3C` | — |
| 1 | 11 | `0808A640` | 4000 | 10000 | `0x3C` | — |
| 1 | 12 | `0808B5F0` | 1472 | 10000 | `0x3C` | — |
| 1 | 13 | `0808BBC0` | 15600 | 12000 | `0x3C` | — |
| 1 | 14 | `0808F8C0` | 10380 | 15000 | `0x3C` | — |
| 1 | 15 | `08092160` | 16664 | 17000 | `0x45` | 6148 |
| 1 | 16 | `08096290` | 18236 | 16000 | `0x45` | 10448 |
| 1 | 17 | `0809A9E0` | 10232 | 16000 | `0x45` | 5572 |
| 1 | 18 | `0809D1F0` | 12204 | 15000 | `0x47` | 6028 |
| 1 | 19 | `080A01B0` | 18440 | 15000 | `0x4D` | 10084 |
| 1 | 20 | `080A49D0` | 14620 | 15000 | `0x47` | 9152 |
| 1 | 21 | `080A85D0` | 3684 | 12000 | `0x3C` | — |
| 1 | 22 | `080A9450` | 13572 | 13000 | `0x3C` | — |
| 1 | 23 | `080AC970` | 5064 | 10000 | `0x3C` | 0 |
| 1 | 24 | `080AE710` | 10704 | 10000 | `0x3C` | — |
| 1 | 25 | `080B10F0` | 12332 | 11000 | `0x3C` | — |
| 1 | 26 | `080B4130` | 11648 | 10000 | `0x3C` | — |
| 1 | 27 | `080B6EC0` | 15092 | 12000 | `0x3C` | — |
| 1 | 28 | `080BA9D0` | 16656 | 10000 | `0x3C` | — |
| 1 | 29 | `080BF1A0` | 13708 | 15000 | `0x3C` | — |
| 1 | 30 | `080C2740` | 15728 | 13000 | `0x3C` | — |
| 1 | 31 | `080C64C0` | 15128 | 15000 | `0x3C` | — |
| 1 | 32 | `080C9FF0` | 16360 | 14000 | `0x3C` | — |
| 1 | 33 | `080CDFF0` | 18528 | 15000 | `0x3C` | — |
| 1 | 34 | `080D2B20` | 10920 | 9000 | `0x3C` | — |
| 1 | 35 | `080D55E0` | 10404 | 13000 | `0x3C` | — |
| 1 | 36 | `080D7EA0` | 8480 | 8000 | `0x3C` | 4156 |
| 1 | 37 | `080D9FD0` | 11384 | 8000 | `0x3C` | — |
| 1 | 38 | `080DCC60` | 5020 | 15000 | `0x3C` | — |
| 1 | 39 | `080DE010` | 1988 | 15000 | `0x3C` | — |
| 1 | 40 | `080DE7F0` | 42600 | 16000 | `0x3C` | — |
| 1 | 41 | `080E8E70` | 17800 | 16000 | `0x3C` | — |
| 1 | 42 | `080EFA90` | 1300 | 15000 | `0x3C` | — |
| 1 | 43 | `080ED410` | 2888 | 12000 | `0x3C` | — |
| 1 | 44 | `080EDF70` | 6928 | 10000 | `0x3C` | 3237 |
| 1 | 45 | `080EFFC0` | 11792 | 12000 | `0x3C` | — |
| 1 | 46 | `080F2DE0` | 13272 | 14000 | `0x3C` | — |
| 1 | 47 | `080F61D0` | 10232 | 15000 | `0x3C` | — |
| 1 | 48 | `080F89E0` | 8236 | 15000 | `0x3C` | — |
| 1 | 49 | `080FAA20` | 13152 | 17000 | `0x3C` | — |
| 1 | 50 | `080FDD90` | 9400 | 14000 | `0x3C` | — |
| 1 | 51 | `08100260` | 8644 | 14000 | `0x3C` | — |
| 1 | 52 | `08102440` | 34208 | 15000 | `0x3C` | — |
| 1 | 53 | `08119E10` | 15028 | 15000 | `0x5B` | 9112 |
| 1 | 54 | `0810B370` | 15904 | 15000 | `0x48` | — |
| 1 | 55 | `0810F9C0` | 9888 | 15000 | `0x3C` | — |
| 1 | 56 | `08112070` | 15160 | 15000 | `0x3C` | — |
| 1 | 57 | `08115BC0` | 9000 | 22000 | `0x3C` | — |
| 1 | 58 | `081184A0` | 6488 | 16000 | `0x3C` | — |
| 1 | 59 | `0811D8E0` | 2124 | 15000 | `0x3C` | — |
| 1 | 60 | `0811F660` | 19472 | 16000 | `0x3C` | — |
| 1 | 61 | `08124280` | 15484 | 16000 | `0x3C` | — |
| 1 | 62 | `08127F10` | 18152 | 16000 | `0x51` | 8608 |
| 1 | 63 | `0812C610` | 23220 | 16000 | `0x4D` | 10004 |
| 2 | 0 | `0806E5D0` | 1400 | 15000 | `0x23` | — |
| 2 | 1 | `0806EB60` | 1900 | 15000 | `0x24` | — |
| 2 | 2 | `0806A320` | 8 | 44100 | `0x3C` | — |
| 2 | 3 | `08072D40` | 6700 | 18000 | `0x26` | — |
| 2 | 4 | `0806A320` | 8 | 44100 | `0x3C` | — |
| 2 | 5 | `08074780` | 7200 | 18000 | `0x28` | — |
| 2 | 6 | `080763B0` | 6400 | 16000 | `0x32` | — |
| 2 | 7 | `0806F2E0` | 2200 | 22000 | `0x2A` | — |
| 2 | 8 | `080763B0` | 6400 | 16000 | `0x32` | — |
| 2 | 9 | `0806FB90` | 2200 | 22000 | `0x2C` | — |
| 2 | 10 | `080763B0` | 6400 | 16000 | `0x32` | — |
| 2 | 11 | `08070440` | 10480 | 16000 | `0x2E` | — |
| 2 | 12 | `080763B0` | 6400 | 16000 | `0x32` | — |
| 2 | 13 | `080763B0` | 6400 | 16000 | `0x32` | — |
| 2 | 14 | `0806A320` | 8 | 44100 | `0x3C` | — |
| 2 | 15 | `080763B0` | 6400 | 16000 | `0x32` | — |

### 12.1 Percussion macros

Complete bytes through first `FF` follow. Durations, volume changes and retriggers belong to the instrument. Empty noise macro and PCM data are separate cases.

The earlier note “Perc 0x24 (bass drum) = unk_807FA92” uses a disassembler's unnamed address label. `0x24` is a sequence note value, not a literal pointer or a universal bass-drum ID. In this image the percussion header's base is `0x23`, so `0x24 − 0x23 = 1`: the noise path reads macro pointer 1 at `0806A340`, selecting **`0806A38B`**. The PCM mode-2 path instead reads sample pointer 1 at `0806A2E4`, selecting **`0806EB60`**, and plays it at pitch `0x24`. These are distinct destinations for the same percussion note. The published `0807FA92` label belongs to the separately analyzed image; its precise data cannot be verified from this ROM. Establishing that destination requires that image's percussion header and pointer entry, rather than assuming the address or the instrument name transfers between builds.

| Index | Address | Complete macro bytes |
| --- | --- | --- |
| 0 | `0806A37D` | `E1 01 E2 4E DE E2 4F DD E1 02 D6 D4 D2 FF` |
| 1 | `0806A38B` | `E1 01 E2 4D DE E2 4E DD E1 02 D6 D4 D2 FF` |
| 2 | `0806A37C` | `FF` |
| 3 | `0806A399` | `E1 01 E2 4F DF E2 3F DE E2 14 E1 03 D7 D5 D3 FF` |
| 4 | `0806A37C` | `FF` |
| 5 | `0806A3A9` | `E1 01 E2 4D DD E2 3F DD E2 14 E1 03 D7 D4 D2 FF` |
| 6 | `0806A37C` | `FF` |
| 7 | `0806A3B9` | `E1 01 E2 11 D5 E2 10 D3 D2 FF` |
| 8 | `0806A37C` | `FF` |
| 9 | `0806A3C3` | `E1 01 E2 13 D6 E2 11 D4 D2 FF` |
| 10 | `0806A37C` | `FF` |
| 11 | `0806A3CD` | `E1 01 E2 12 D6 E1 06 E2 11 D4 D3 E1 04 D2 FF` |
| 12 | `0806A37C` | `FF` |
| 13 | `0806A37C` | `FF` |
| 14 | `0806A37C` | `FF` |
| 15 | `0806A37C` | `FF` |

## 13. Evidence and verification boundaries

Layouts and behavior were checked against this image's Thumb instructions/literals. Independent execution across eighteen music entries compared **99,912 state frames**, **93,480 PSG register frames**, **6,432 noise register frames**, without discrepancies in compared fields/writes. This covers exercised music paths, not every command combination.

Full-game mGBA 0.10.2 execution supplied song 10 and six individually isolated physical voices while the full song continued. Accounting for two scheduling holds, **17,280 voice frames matched across five RAM fields**. This is emulator execution, not physical-console recording. It establishes checked state progression, not sample-for-sample identity of a separate audio model.

| Area | Evidence boundary |
| --- | --- |
| Other songs | All eighteen received state/register checks; only song 10 has the complete six-voice audio comparison described here |
| Active sweep/nondefault routing | ROM paths identified; absent from music and lacking equivalent full-game coverage |
| Mode 1/effect allocation | Direct forms/priority established by instructions; outside all-music comparisons |
| Auxiliary noise | Initialization/selection established; role/all interactions unresolved |
| Nonzero scale | Unused here; suspect lookup prevents reliable general formula |
| Extreme/invalid operands | Overreads, wrap and unchecked indices exist; ordinary checks do not certify malformed streams |
| PCM ending phase | Mechanism established; fixed extra frame specific to observed scheduling |
| Final waveform | Requires phase, sub-frame events, FIFO buffering, quantization, shared clipping/bias and sampling/PWM |
| Analog output | No physical recording establishes filtering/model differences here |

Onsets and slow loudness correlation are insufficient fidelity tests. Wrong wave fundamental can retain similar energy; softened pulses retain notes but lose harmonics; incorrect noise retains its level curve. Compare bytes, timing, writes, pitch, retriggers, loops, levels and spectrum independently. Final gain must not conceal incorrect PSG/PCM balance.

## 14. Related analysis and hardware references

[Loveemu's Hachiemon format notes](https://gist.github.com/loveemu/1e53d09b4540b3d5ccf93128ea463080) provide earlier analysis of masks, commands and delayed notes. Their example uses different addresses/record size; this reference identifies its exact image and derives layout separately.

[GBATEK](https://problemkaputt.de/gbatek.htm), also available as [GBA-only text](https://rust-console.github.io/gbatek-gbaonly/), describes GBA registers, DMA, timers and output sampling. [Pan Docs](https://gbdev.io/pandocs/Audio_Registers.html) covers related pulse/noise behavior; GBA banking/routing/output require GBA documentation. [mGBA GBA audio](https://github.com/mgba-emu/mgba/blob/0.10.2/src/gba/audio.c) and [PSG audio](https://github.com/mgba-emu/mgba/blob/0.10.2/src/gb/audio.c) describe emulator behavior relevant to these observations.
