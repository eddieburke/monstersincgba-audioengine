#include "hachiemon.h"

#define REG8(a) (*(volatile u8 *)(a))
#define REG16(a) (*(volatile u16 *)(a))
#define REG32(a) (*(volatile u32 *)(a))

#define REG_SOUND1CNT_L 0x04000060UL
#define REG_SOUND1CNT_H 0x04000062UL
#define REG_SOUND1CNT_X 0x04000064UL
#define REG_SOUND2CNT_L 0x04000068UL
#define REG_SOUND2CNT_H 0x0400006CUL
#define REG_SOUND3CNT_L 0x04000070UL
#define REG_SOUND3CNT_H 0x04000072UL
#define REG_SOUND3CNT_X 0x04000074UL
#define REG_SOUND4CNT_L 0x04000078UL
#define REG_SOUND4CNT_H 0x0400007CUL
#define REG_SOUNDCNT_L 0x04000080UL
#define REG_SOUNDCNT_H 0x04000082UL
#define REG_SOUNDCNT_X 0x04000084UL
#define REG_SOUNDBIAS 0x04000088UL
#define REG_WAVE_RAM 0x04000090UL
#define REG_FIFO_A 0x040000A0UL
#define REG_FIFO_B 0x040000A4UL
#define REG_DMA1SAD 0x040000BCUL
#define REG_DMA1DAD 0x040000C0UL
#define REG_DMA1CNT 0x040000C4UL
#define REG_DMA2SAD 0x040000C8UL
#define REG_DMA2DAD 0x040000CCUL
#define REG_DMA2CNT 0x040000D0UL
#define REG_TM0CNT 0x04000100UL
#define REG_TM1CNT 0x04000104UL
#define ROM_NOTE_TABLE ((const u16 *)0x08132108UL)
#define ROM_WAVE_TABLE ((const u8 *)0x0806944CUL)
#define ROM_WAVE_LEVEL ((const u8 *)0x0813219AUL)
#define ROM_Q12_TABLE ((const u16 *)0x081321F6UL)
#define ROM_PSG_ROUTE_MASK ((const u8 *)0x081321AAUL)
#define ROM_PSG_ROUTE_DATA ((const u8 *)0x081321AEUL)
#define ROM_PCM_ROUTE_MASK ((const u8 *)0x081321BEUL)
#define ROM_PCM_ROUTE_DATA ((const u8 *)0x081321C2UL)
#define ROM_PCM_LEVEL ((const u8 *)0x081321D0UL)

#define CH_DIRTY_FREQ 0x01
#define CH_DIRTY_VOL 0x02
#define CH_DIRTY_DUTY 0x04
#define CH_DIRTY_SWEEP 0x08
#define CH_DIRTY_START 0x10
#define CH_DIRTY_WAVE 0x20
#define CH_DIRTY_ROUTE 0x40

HachiContext hachiemon;

typedef char HachiChannelSizeCheck[(sizeof(HachiChannel) == 0x64) ? 1 : -1];
typedef char HachiEchoSizeCheck[(sizeof(HachiEcho) == 0x34) ? 1 : -1];
typedef char HachiPcmSizeCheck[(sizeof(HachiPcm) == 0x24) ? 1 : -1];
typedef char HachiContextSizeCheck[(sizeof(HachiContext) == 0x614) ? 1 : -1];

static const u8 engineBySlot[13] = {0, 1, 2, 3, 0, 1, 2, 3, 4, 5, 4, 5, 3};

static u8 Read8(const u8 *p)
{
    return *p++;
}

static u16 Read16(const u8 *p)
{
    return (u16)(p[0] | ((u16)p[1] << 8));
}

static u32 Read32(const u8 *p)
{
    return (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24);
}

static s32 Signed32(u32 value)
{
    if (value & 0x80000000UL)
        return -1 - (s32)(~value);
    return (s32)value;
}

static s8 Signed8(u8 value)
{
    if (value & 0x80)
        return (s8)(value - 0x100);
    return (s8)value;
}

static u16 ClampU16(s32 value, u16 high)
{
    if (value < 0)
        return 0;
    if (value > high)
        return high;
    return (u16)value;
}

static u32 DivideSigned(s32 numerator, s32 denominator)
{
    if (denominator == 0)
        return 0;
    return (u32)(numerator / denominator);
}

static u16 NoteFrequency(u16 index)
{
    return ROM_NOTE_TABLE[index];
}

static u32 Q12Multiplier(s32 semitones)
{
    u32 octave;
    u32 remainder;
    u32 value;
    if (semitones >= 0) {
        octave = (u32)semitones / 12;
        remainder = (u32)semitones % 12;
        value = ROM_Q12_TABLE[remainder];
        return octave < 20 ? value << octave : 0x7FFFFFFFUL;
    }
    semitones = -semitones;
    octave = (u32)semitones / 12;
    remainder = (u32)semitones % 12;
    if (remainder == 0)
        return 4096UL >> octave;
    value = ROM_Q12_TABLE[12 - remainder];
    return value >> (octave + 1);
}

static u32 PcmRate(HachiPcm *voice, s32 pitch)
{
    u32 rate;
    u32 multiplier;
    u8 scale;
    rate = Read16(voice->sample + 4);
    multiplier = Q12Multiplier(pitch - (s32)Read8(voice->sample + 7));
    rate = (rate * multiplier) >> 12;
    scale = Read8(voice->sample + 6);
    if (scale != 0)
        rate = (rate * Q12Multiplier((s8)scale)) >> 12;
    if (rate > 0x78000UL)
        rate = 0x78000UL;
    return rate;
}

static u32 TimerWord(u32 rate)
{
    u32 count;
    if (rate == 0)
        return 0x00800000UL;
    count = 0x01000000UL / rate;
    return 0x00800000UL | ((0x10000UL - count) & 0xFFFFUL);
}

static void StopDma(u8 voice)
{
    if (voice == 1) {
        REG32(REG_DMA1CNT) = 0;
        REG16(REG_TM0CNT + 2) = 0;
        REG16(REG_TM0CNT) = 0;
        REG16(REG_SOUNDCNT_H) = (u16)(REG16(REG_SOUNDCNT_H) & ~0x0300);
    } else {
        REG32(REG_DMA2CNT) = 0;
        REG16(REG_TM1CNT + 2) = 0;
        REG16(REG_TM1CNT) = 0;
        REG16(REG_SOUNDCNT_H) = (u16)(REG16(REG_SOUNDCNT_H) & ~0x3000);
    }
}

static void StartDma(HachiPcm *voice, const u8 *source, u8 prime)
{
    u32 fifo;
    u32 dmaSource;
    u32 dmaDest;
    u32 dmaControl;
    u32 timer;
    u16 timerReload;
    u8 channel;
    channel = (u8)(voice->voice - 1);
    fifo = channel == 0 ? REG_FIFO_A : REG_FIFO_B;
    dmaSource = channel == 0 ? REG_DMA1SAD : REG_DMA2SAD;
    dmaDest = channel == 0 ? REG_DMA1DAD : REG_DMA2DAD;
    dmaControl = channel == 0 ? REG_DMA1CNT : REG_DMA2CNT;
    timer = channel == 0 ? REG_TM0CNT : REG_TM1CNT;
    if (prime) {
        StopDma(voice->voice);
        REG16(REG_SOUNDCNT_H) = (u16)(REG16(REG_SOUNDCNT_H) | (channel == 0 ? 0x0800 : 0x8000));
        for (timerReload = 0; timerReload < 8; timerReload++)
            REG32(fifo) = Read32(source + timerReload * 4);
        source += 0x20;
    }
    REG32(dmaSource) = (u32)source;
    REG32(dmaDest) = fifo;
    REG32(dmaControl) = 0xF6400004UL;
    if (prime)
        REG32(timer) = voice->timer;
}

static void PcmStop(HachiPcm *voice)
{
    if (voice->sample != 0)
        StopDma(voice->voice);
    voice->sample = 0;
    voice->rate = 0;
    voice->position = 0;
    voice->flags = 0;
    voice->timer = 0;
    voice->rateOffset = 0;
    voice->life = 0;
    voice->age = 0;
    voice->pendingAge = 0;
}

static void PcmStart(HachiPcm *voice, const u8 *sample, u16 pitch, u8 level, u16 life, u8 direct)
{
    u32 multiplier;
    s32 rate;
    if (sample == 0 || level == 0) {
        PcmStop(voice);
        return;
    }
    voice->sample = sample;
    voice->position = 0;
    voice->flags &= ~1UL;
    voice->life = life;
    voice->age = 0;
    voice->pendingAge = 0;
    voice->pitch = direct ? 0 : (u8)pitch;
    voice->volume = level;
    if (direct) {
        multiplier = pitch;
        rate = (s32)(((u32)Read16(sample + 4) * multiplier) >> 12);
        if (rate > 0x78000L)
            rate = 0x78000L;
    } else {
        voice->rate = PcmRate(voice, pitch);
        rate = (s32)voice->rate;
    }
    voice->rate = (u32)rate;
    voice->timer = TimerWord(voice->rate);
    if (voice->voice == 1)
        REG16(REG_SOUNDCNT_H) = (u16)((REG16(REG_SOUNDCNT_H) & ~0x0004) | (level >= 8 ? 0x0004 : 0));
    else
        REG16(REG_SOUNDCNT_H) = (u16)((REG16(REG_SOUNDCNT_H) & ~0x0008) | (level >= 8 ? 0x0008 : 0));
    StartDma(voice, sample + 0x10, 1);
    voice->flags &= ~2UL;
}

static u32 PcmRateFromNote(HachiPcm *voice, s32 pitch)
{
    return PcmRate(voice, pitch);
}

static void SetVolume(HachiChannel *channel, u8 volume)
{
    channel->volume = volume;
    channel->flags |= CH_DIRTY_VOL;
    if (channel->engine == 3)
        channel->flags |= CH_DIRTY_FREQ;
}

static u16 LevelProduct(HachiChannel *channel)
{
    channel->levelProduct = (u16)(((u32)channel->gain * channel->volume) & 0xFFFFF000UL);
    return channel->levelProduct;
}

static u8 EnvelopeGate(HachiChannel *channel);
static void EnvelopeSetup1(HachiChannel *channel);
static void EnvelopeSetup2(HachiChannel *channel);
static void EnvelopeSetup3(HachiChannel *channel);
static void EnvelopeSetup4(HachiChannel *channel);
static void EnvelopeSetup5(HachiChannel *channel);
static void EnvelopeTick(HachiChannel *channel);
static void ModulationSetup(HachiChannel *channel);
static void ModulationTick(HachiChannel *channel);
static void ChannelTick(HachiChannel *channel);
static void ChannelWrite(HachiChannel *channel);
static void PcmApply(HachiChannel *channel, HachiPcm *voice);
static void WriteRoute(HachiChannel *channel);

static void ChannelInit(HachiChannel *channel, const u8 *stream)
{
    u8 i;
    if (channel->echo != 0)
        channel->echo->cursor = 0;
    channel->stream = stream;
    channel->loop1 = 0;
    channel->loop2 = 0;
    channel->returnPtr = 0;
    channel->macro = 0;
    channel->wait = 0;
    channel->baseDuration = 0;
    channel->macroWait = 0;
    channel->flags = 0x5F;
    channel->loop1Count = 0;
    channel->loop2Count = 0;
    channel->sweep = 8;
    channel->offset = 0;
    channel->modulationDelay = 0;
    channel->modulationPeriod = 0;
    channel->modulationDepth = 0;
    channel->modulationDelayLeft = 0;
    channel->modulationPeriodLeft = 0;
    channel->modulationDelta = 0;
    channel->modulation = 0;
    channel->mode = 0;
    channel->envelopeEnabled = 0;
    channel->note = 0xFFFF;
    channel->savedVolume = 15;
    for (i = 0; i < 6; i++)
        ((u8 *)channel)[0x5A + i] = 0;
    channel->echoNote = 0xFF;
    channel->priority = 0;
    channel->durationHigh = 0;
    channel->gain = 0x1000;
    channel->targetGain = 0x1000;
    channel->gainStep = 0;
    channel->rampWait = 0;
    channel->routing = 3;
}

static void EnvelopeEnd(HachiChannel *channel)
{
    channel->envelopeState = 0;
    SetVolume(channel, 0);
}

static u8 EnvelopeGate(HachiChannel *channel)
{
    if ((u32)channel->releaseWindow >= channel->wait) {
        EnvelopeSetup5(channel);
        if (channel->envelopeState == 5) {
            channel->envelopeCount = (u8)(channel->envelopeCount - 1);
            if (channel->envelopeCount == 0) {
                if (channel->envelopeStep == 0) {
                    EnvelopeEnd(channel);
                } else {
                    channel->envelopeStep = (u8)(channel->envelopeStep - 1);
                    if (channel->volume != 0)
                        SetVolume(channel, (u8)(channel->volume - 1));
                    channel->envelopeCount = channel->release >> 4;
                }
            }
        }
        return 1;
    }
    return 0;
}

static void EnvelopeSetup1(HachiChannel *channel)
{
    if (!EnvelopeGate(channel)) {
        channel->envelopeCount = 1;
        channel->envelopeStep = 0;
        channel->envelopeState = 1;
    }
}

static void EnvelopeSetup2(HachiChannel *channel)
{
    if (EnvelopeGate(channel))
        return;
    if (channel->attack >> 4) {
        channel->envelopeCount = channel->attack >> 4;
        channel->envelopeStep = channel->attack & 15;
        channel->envelopeState = 2;
    } else {
        EnvelopeSetup3(channel);
    }
}

static void EnvelopeSetup3(HachiChannel *channel)
{
    if (EnvelopeGate(channel))
        return;
    if (channel->decay >> 4) {
        channel->envelopeCount = channel->decay >> 4;
        channel->envelopeStep = (u8)(-(s8)(channel->decay & 15));
        channel->envelopeState = 3;
    } else {
        EnvelopeSetup4(channel);
    }
}

static void EnvelopeSetup4(HachiChannel *channel)
{
    if (EnvelopeGate(channel))
        return;
    if (channel->releaseWindow == 0) {
        EnvelopeSetup5(channel);
    } else {
        channel->envelopeState = 4;
        channel->envelopeCount = channel->releaseWindow;
    }
}

static void EnvelopeSetup5(HachiChannel *channel)
{
    if ((channel->release >> 4) == 0) {
        EnvelopeEnd(channel);
    } else {
        channel->envelopeCount = 1;
        channel->envelopeStep = channel->release & 15;
        channel->envelopeState = 5;
    }
}

static void EnvelopeTick(HachiChannel *channel)
{
    u8 state;
    s8 step;
    if (channel->envelopeEnabled == 0)
        return;
    state = channel->envelopeState;
    if (state == 1) {
        channel->envelopeCount = (u8)(channel->envelopeCount - 1);
        if (channel->envelopeCount == 0)
            EnvelopeSetup2(channel);
    } else if (state == 2) {
        channel->envelopeCount = (u8)(channel->envelopeCount - 1);
        if (channel->envelopeCount == 0xFF) {
            EnvelopeSetup3(channel);
        } else {
            SetVolume(channel, ClampU16((s32)channel->volume + (channel->attack & 15), 15));
        }
    } else if (state == 3) {
        channel->envelopeCount = (u8)(channel->envelopeCount - 1);
        if (channel->envelopeCount == 0xFF) {
            EnvelopeSetup4(channel);
        } else {
            step = Signed8(channel->envelopeStep);
            SetVolume(channel, ClampU16((s32)channel->volume + step, 15));
        }
    } else if (state == 4) {
        EnvelopeGate(channel);
    } else if (state == 5) {
        channel->envelopeCount = (u8)(channel->envelopeCount - 1);
        if (channel->envelopeCount == 0) {
            if (channel->envelopeStep == 0) {
                EnvelopeEnd(channel);
            } else {
                channel->envelopeStep = (u8)(channel->envelopeStep - 1);
                if (channel->volume != 0)
                    SetVolume(channel, (u8)(channel->volume - 1));
                channel->envelopeCount = channel->release >> 4;
            }
        }
    }
}

static void ModulationSetup(HachiChannel *channel)
{
    u16 index;
    s32 span;
    s32 numerator;
    if (channel->engine <= 2) {
        channel->modulationDelayLeft = channel->modulationDelay;
        channel->modulationPeriodLeft = channel->modulationPeriod >> 1;
        index = channel->note;
        span = (s32)(u16)NoteFrequency((u16)(index + 2)) - (s32)NoteFrequency(index);
        numerator = (s32)((u32)span * channel->modulationDepth);
        channel->modulationDelta = (s32)DivideSigned(numerator, channel->modulationPeriodLeft);
        channel->modulation = 0;
    } else if (channel->engine >= 4) {
        HachiPcm *voice;
        s32 pitch;
        voice = &hachiemon.pcm[channel->slot & 1];
        channel->modulationDelayLeft = channel->modulationDelay;
        channel->modulationPeriodLeft = channel->modulationPeriod >> 1;
        pitch = (s32)channel->note;
        if (channel->mode == 2)
            pitch += Read16(hachiemon.percussionTable + 2);
        else
            pitch += channel->transpose;
        span = (s32)PcmRateFromNote(voice, pitch + 2) - (s32)PcmRateFromNote(voice, pitch);
        numerator = (s32)((u32)span * channel->modulationDepth);
        channel->modulationDelta = (s32)DivideSigned(numerator, channel->modulationPeriodLeft);
        channel->modulation = 0;
    }
}

static void ModulationTick(HachiChannel *channel)
{
    HachiPcm *voice;
    if (channel->modulationDepth == 0)
        return;
    if (channel->modulationDelayLeft != 0) {
        channel->modulationDelayLeft--;
        return;
    }
    channel->modulation = Signed32((u32)channel->modulation + (u32)channel->modulationDelta);
    channel->flags |= CH_DIRTY_FREQ;
    if (channel->engine >= 4) {
        voice = &hachiemon.pcm[channel->slot & 1];
        voice->rateOffset = channel->modulation;
        voice->flags |= 2;
    }
    if (channel->modulationPeriodLeft != 0) {
        channel->modulationPeriodLeft--;
    } else {
        channel->modulationPeriodLeft = channel->modulationPeriod;
        channel->modulationDelta = Signed32(0U - (u32)channel->modulationDelta);
    }
}

static u16 EffectiveFrequency(HachiChannel *channel)
{
    s32 value;
    value = (s16)(u16)(channel->frequency + channel->offset + (channel->modulation >> 4));
    return ClampU16(value, 0x07FF);
}

static void StartNoiseMacro(HachiChannel *channel, u8 note)
{
    const u8 *table;
    u16 count;
    u16 base;
    u16 index;
    table = hachiemon.percussionTable;
    count = Read16(table);
    base = Read16(table + 2);
    if (count == 0)
        return;
    if ((s32)note < (s32)base)
        index = 0;
    else {
        index = (u16)(note - base);
        if (index >= count)
            index = (u16)(count - 1);
    }
    channel->note = index;
    channel->echoNote = 0xFF;
    channel->frequency = 0;
    channel->macro = (const u8 *)Read32(table + 4 + index * 4);
    channel->macroWait = 0;
    channel->flags |= CH_DIRTY_VOL | CH_DIRTY_START;
}

static void NoteStart(HachiChannel *channel, u8 note)
{
    u8 duration;
    u16 raw;
    s32 index;
    duration = Read8(channel->stream++);
    channel->wait = (((u32)channel->durationHigh << 8) | duration) + 1;
    channel->durationHigh = 0;
    channel->echoNote = 0xFF;
    channel->flags |= CH_DIRTY_FREQ | CH_DIRTY_START;
    channel->volume = channel->savedVolume;
    channel->flags |= CH_DIRTY_VOL;
    if (channel->mode == 1) {
        channel->wait = channel->baseDuration;
        if (channel->engine <= 2) {
            raw = (u16)(((u16)note << 8) | Read8(channel->stream++));
            channel->note = 0;
            channel->frequency = raw;
            channel->flags |= 0x13;
            return;
        }
        if (channel->engine >= 4) {
            raw = (u16)(((u16)note << 8) | Read8(channel->stream++));
            channel->note = raw;
            channel->flags |= 0x10;
            return;
        }
    }
    if (channel->engine <= 2) {
        if (channel->mode == 2)
            return;
        index = (s32)note + channel->transpose - 0x24;
        if (index < 0)
            index = 0;
        if (index > 0x47)
            index = 0x47;
        channel->note = (u16)index;
        channel->frequency = NoteFrequency((u16)index);
        ModulationSetup(channel);
        EnvelopeSetup1(channel);
        return;
    }
    if (channel->engine == 3) {
        if (channel->mode == 1)
            return;
        StartNoiseMacro(channel, note);
        return;
    }
    if (channel->mode == 2) {
        u16 count;
        u16 base;
        u16 drum;
        count = Read16(hachiemon.percussionTable);
        base = Read16(hachiemon.percussionTable + 2);
        if (count == 0)
            return;
        if ((s32)note < (s32)base)
            drum = 0;
        else {
            drum = (u16)(note - base);
            if (drum >= count)
                drum = (u16)(count - 1);
        }
        channel->note = drum;
    } else {
        channel->note = note;
    }
    channel->flags |= CH_DIRTY_START;
}

static void NoteOff(HachiChannel *channel, u8 duration)
{
    channel->wait = (u32)duration + 1;
    channel->note = 0x00FF;
    channel->frequency = 0;
    channel->echoNote = 0xFF;
    SetVolume(channel, 0);
    if (channel->engine == 3)
        channel->flags |= CH_DIRTY_START;
}

static void DrumMacroTick(HachiChannel *channel)
{
    u8 command;
    if (channel->macro == 0)
        return;
    if (channel->macroWait != 0)
        channel->macroWait--;
    while (channel->macro != 0 && channel->macroWait == 0) {
        command = Read8(channel->macro++);
        if ((command & 0xF0) == 0xD0) {
            channel->savedVolume = command & 15;
            SetVolume(channel, command & 15);
            channel->macroWait = channel->baseDuration;
        } else if (command == 0xE1) {
            channel->baseDuration = Read8(channel->macro++);
        } else if (command == 0xE2) {
            channel->parameter = Read8(channel->macro++);
            channel->flags |= CH_DIRTY_FREQ | CH_DIRTY_START;
        } else if (command == 0xFF) {
            channel->savedVolume = 0;
            SetVolume(channel, 0);
            channel->macro = 0;
        }
    }
}

static void Command(HachiChannel *channel)
{
    u8 command;
    u8 a;
    u32 target;
    command = Read8(channel->stream++);
    if (command < 0x80) {
        channel->stream--;
        NoteStart(channel, Read8(channel->stream++));
        return;
    }
    if (command < 0x90 || command == 0xE0) {
        a = Read8(channel->stream++);
        NoteOff(channel, a);
        return;
    }
    if (command < 0xD0)
        return;
    if (command < 0xE0) {
        channel->savedVolume = command & 15;
        SetVolume(channel, command & 15);
        return;
    }
    switch (command) {
    case 0xE1:
        channel->baseDuration = Read8(channel->stream++);
        break;
    case 0xE2:
        channel->parameter = Read8(channel->stream++);
        channel->flags |= CH_DIRTY_FREQ | CH_DIRTY_START;
        break;
    case 0xE3:
        channel->instrument = Read8(channel->stream++);
        channel->flags |= CH_DIRTY_WAVE;
        break;
    case 0xE4:
    case 0xE5:
    case 0xE6:
    case 0xE7:
        channel->instrument = (u8)((command - 0xE4) << 6);
        channel->flags |= CH_DIRTY_DUTY;
        break;
    case 0xE8:
        channel->transpose = Signed8(Read8(channel->stream++));
        break;
    case 0xE9:
        channel->echoDelay = Read8(channel->stream++);
        if (channel->echoDelay > 0x30)
            channel->echoDelay = 0x30;
        break;
    case 0xEA:
        channel->routing = Read8(channel->stream++);
        channel->flags |= CH_DIRTY_ROUTE;
        break;
    case 0xF0:
        channel->loop1Count = Read8(channel->stream++);
        channel->loop1 = channel->stream;
        break;
    case 0xF1:
        channel->loop1Count--;
        if (channel->loop1Count != 0)
            channel->stream = channel->loop1;
        break;
    case 0xF2:
        channel->loop2Count = Read8(channel->stream++);
        channel->loop2 = channel->stream;
        break;
    case 0xF3:
        channel->loop2Count--;
        if (channel->loop2Count != 0)
            channel->stream = channel->loop2;
        break;
    case 0xF4:
        target = Read32(channel->stream);
        channel->stream += 4;
        channel->stream = (const u8 *)target;
        break;
    case 0xF5:
        target = Read32(channel->stream);
        channel->stream += 4;
        channel->returnPtr = channel->stream;
        channel->stream = (const u8 *)target;
        break;
    case 0xF6:
        channel->stream = channel->returnPtr;
        break;
    case 0xF7:
        channel->sweep = Read8(channel->stream++);
        if (channel->sweep == 0)
            channel->sweep = 8;
        channel->flags |= CH_DIRTY_SWEEP;
        break;
    case 0xF8:
        channel->offset = Read8(channel->stream++);
        break;
    case 0xF9:
        channel->modulationDelay = Read8(channel->stream++);
        a = Read8(channel->stream++);
        channel->modulationPeriod = a & 15;
        channel->modulationDepth = a >> 4;
        break;
    case 0xFA:
        channel->durationHigh = Read8(channel->stream++);
        break;
    case 0xFB:
        channel->attack = Read8(channel->stream++);
        channel->decay = Read8(channel->stream++);
        channel->releaseWindow = Read8(channel->stream++);
        channel->release = Read8(channel->stream++);
        channel->envelopeEnabled = 1;
        if (channel->engine <= 2)
            EnvelopeSetup1(channel);
        break;
    case 0xFC:
        channel->envelopeEnabled = 1;
        if (channel->engine <= 2)
            EnvelopeSetup1(channel);
        break;
    case 0xFD:
        channel->envelopeEnabled = 0;
        if (channel->engine <= 2)
            EnvelopeSetup1(channel);
        break;
    case 0xFE:
        channel->mode = Read8(channel->stream++);
        break;
    case 0xFF:
        HachiemonStopChannel(channel->slot);
        break;
    default:
        if ((command & 0xF0) >= 0xE0 && (command & 0xF0) <= 0xF0) {
            channel->stream--;
            NoteStart(channel, Read8(channel->stream++));
        }
        break;
    }
}

static void ChannelTick(HachiChannel *channel)
{
    u32 guard;
    if (channel->stream == 0)
        return;
    channel->durationHigh = 0;
    if (channel->wait != 0)
        channel->wait--;
    guard = 0;
    while (channel->wait == 0 && channel->stream != 0 && guard++ < 4096)
        Command(channel);
    if (channel->engine <= 2) {
        ModulationTick(channel);
        EnvelopeTick(channel);
    } else if (channel->engine == 3) {
        DrumMacroTick(channel);
    } else {
        ModulationTick(channel);
    }
    if (channel->rampWait != 0) {
        channel->rampWait--;
        if (channel->rampWait != 0) {
            channel->gain = (u16)(channel->gain + channel->gainStep);
            channel->flags |= CH_DIRTY_VOL;
        } else {
            channel->gain = channel->targetGain;
            channel->gainStep = 0;
            HachiemonStopChannel(channel->slot);
        }
    }
}

static void WritePulse(HachiChannel *channel, u32 volumeRegister, u32 frequencyRegister, u8 hasSweep)
{
    u16 level;
    u16 value;
    u16 frequency;
    if (hasSweep && (channel->flags & CH_DIRTY_SWEEP))
        REG16(volumeRegister - 2) = channel->sweep;
    if (channel->flags & (CH_DIRTY_VOL | CH_DIRTY_DUTY)) {
        level = LevelProduct(channel);
        value = (u16)(level | (channel->instrument & 0xC0));
        REG16(volumeRegister) = value;
    }
    if (channel->flags & (CH_DIRTY_FREQ | CH_DIRTY_START | CH_DIRTY_VOL | CH_DIRTY_DUTY)) {
        if (channel->flags & CH_DIRTY_FREQ)
            frequency = EffectiveFrequency(channel);
        else
            frequency = channel->frequency & 0x07FF;
        REG16(frequencyRegister) = (u16)(frequency | 0x8000);
    }
    channel->flags = 0;
}

static void WriteWave(HachiChannel *channel)
{
    u32 i;
    u16 frequency;
    u16 waveControl;
    if (channel->flags & CH_DIRTY_WAVE) {
        waveControl = REG16(REG_SOUND3CNT_L);
        for (i = 0; i < 8; i++)
            REG16(REG_WAVE_RAM + i * 2) = Read16(ROM_WAVE_TABLE + (channel->instrument & 3) * 16 + i * 2);
        REG16(REG_SOUND3CNT_L) = waveControl == 0x0080 ? 0x00C0 : 0x0080;
    }
    if (channel->flags & CH_DIRTY_VOL) {
        frequency = LevelProduct(channel);
        REG16(REG_SOUND3CNT_H) = (u16)(((u16)ROM_WAVE_LEVEL[(frequency >> 12) & 15] << 8));
    }
    if (channel->flags & (CH_DIRTY_FREQ | CH_DIRTY_START)) {
        if (channel->flags & CH_DIRTY_FREQ)
            frequency = EffectiveFrequency(channel);
        else
            frequency = channel->frequency & 0x07FF;
        REG16(REG_SOUND3CNT_X) = (u16)(frequency | 0x8000);
    }
    channel->flags = 0;
}

static void WriteNoise(HachiChannel *channel)
{
    if (channel->flags & CH_DIRTY_VOL)
        REG16(REG_SOUND4CNT_L) = LevelProduct(channel);
    if (channel->flags & (CH_DIRTY_FREQ | CH_DIRTY_START))
        REG16(REG_SOUND4CNT_H) = (u16)(channel->parameter | 0x8000);
    channel->flags = 0;
}

static void ChannelWrite(HachiChannel *channel)
{
    WriteRoute(channel);
    if (channel->engine == 0)
        WritePulse(channel, REG_SOUND1CNT_H, REG_SOUND1CNT_X, 1);
    else if (channel->engine == 1)
        WritePulse(channel, REG_SOUND2CNT_L, REG_SOUND2CNT_H, 0);
    else if (channel->engine == 2)
        WriteWave(channel);
    else if (channel->engine == 3)
        WriteNoise(channel);
    else
        PcmApply(channel, &hachiemon.pcm[channel->slot & 1]);
}

static void WriteRoute(HachiChannel *channel)
{
    u8 engine;
    u8 mask;
    u8 data;
    u16 control;
    if ((channel->flags & CH_DIRTY_ROUTE) == 0)
        return;
    engine = channel->engine;
    if (engine < 4) {
        mask = ROM_PSG_ROUTE_MASK[engine];
        data = ROM_PSG_ROUTE_DATA[engine * 4 + channel->routing];
        control = REG16(REG_SOUNDCNT_L);
        REG16(REG_SOUNDCNT_L) = (u16)((control & mask) | data);
    } else {
        engine = (u8)(engine - 4);
        mask = ROM_PCM_ROUTE_MASK[engine];
        data = ROM_PCM_ROUTE_DATA[engine * 4 + channel->routing];
        control = REG16(REG_SOUNDCNT_H);
        REG16(REG_SOUNDCNT_H) = (u16)((control & mask) | data);
    }
    channel->flags &= (u8)~CH_DIRTY_ROUTE;
}

static void WriteEcho(HachiChannel *channel)
{
    u8 cursor;
    u8 index;
    u8 note;
    u16 frequency;
    u32 volumeRegister;
    u32 frequencyRegister;
    if (channel->echo == 0 || channel->volume != 0 || channel->echoDelay == 0)
        return;
    cursor = channel->echo->cursor;
    index = (u8)((cursor - channel->echoDelay) & 47);
    note = channel->echo->notes[index];
    if (note == channel->echoNote)
        return;
    channel->echoNote = note;
    if (channel->engine > 2)
        return;
    if (channel->engine == 0) {
        volumeRegister = REG_SOUND1CNT_H;
        frequencyRegister = REG_SOUND1CNT_X;
    } else if (channel->engine == 1) {
        volumeRegister = REG_SOUND2CNT_L;
        frequencyRegister = REG_SOUND2CNT_H;
    } else {
        volumeRegister = REG_SOUND3CNT_H;
        frequencyRegister = REG_SOUND3CNT_X;
    }
    if (note == 0xFF) {
        if (channel->engine == 2)
            REG16(volumeRegister) = 0;
        else
            REG16(volumeRegister) = 0;
        return;
    }
    frequency = (u16)(NoteFrequency(note) + channel->offset);
    frequency = ClampU16((s16)frequency, 0x07FF);
    if (channel->engine == 2) {
        REG16(volumeRegister) = (u16)(ROM_WAVE_LEVEL[3] << 8);
    } else {
        REG16(volumeRegister) = (u16)((3 << 12) | (channel->instrument & 0xC0));
    }
    REG16(frequencyRegister) = (u16)(frequency | 0x8000);
}

static HachiChannel *PhysicalChannel(u8 slot)
{
    HachiChannel *music;
    HachiChannel *effect;
    if (slot <= 2) {
        music = &hachiemon.channel[slot];
        effect = &hachiemon.channel[slot + 4];
        if (effect->stream != 0)
            return effect;
        return (hachiemon.masterFlags & 2) ? 0 : music;
    }
    if (slot == 8 || slot == 9) {
        music = &hachiemon.channel[slot];
        effect = &hachiemon.channel[slot + 2];
        if (effect->stream != 0)
            return effect;
        return (hachiemon.masterFlags & 2) ? 0 : music;
    }
    if (slot == 3) {
        music = &hachiemon.channel[3];
        effect = &hachiemon.channel[7];
        if (effect->stream != 0)
            return effect;
        if (hachiemon.masterFlags & 2)
            return 0;
        if (hachiemon.channel[12].stream != 0 && hachiemon.channel[9].stream != 0 &&
            hachiemon.channel[9].mode == 2 && hachiemon.channel[11].stream != 0)
            return &hachiemon.channel[12];
        return music;
    }
    return 0;
}

static void PcmApply(HachiChannel *channel, HachiPcm *voice)
{
    const u8 *sample;
    u16 pitch;
    u16 level;
    u16 drum;
    u32 life;
    s32 signedPitch;
    if ((channel->flags & CH_DIRTY_START) == 0)
        return;
    level = (u16)(LevelProduct(channel) >> 12);
    if (level == 0) {
        PcmStop(voice);
        channel->flags &= (u8)~CH_DIRTY_START;
        return;
    }
    if (channel->mode == 1) {
        sample = (const u8 *)Read32(hachiemon.pcmTable1 + channel->instrument * 4);
        pitch = channel->note;
        voice->voice = (u8)((channel->slot & 1) + 1);
        life = channel->wait;
        if (channel->envelopeEnabled) {
            if (life > channel->release)
                life -= channel->release;
            else
                life = 1;
        }
        PcmStart(voice, sample, pitch, (u8)level, (u16)life, 1);
    } else if (channel->mode == 2) {
        drum = channel->note;
        sample = (const u8 *)Read32(hachiemon.pcmTable2 + drum * 4);
        pitch = (u16)(Read16(hachiemon.percussionTable + 2) + drum);
        voice->voice = (u8)((channel->slot & 1) + 1);
        life = channel->wait;
        if (channel->envelopeEnabled) {
            if (life > channel->release)
                life -= channel->release;
            else
                life = 1;
        }
        PcmStart(voice, sample, pitch, (u8)level, (u16)life, 0);
    } else {
        sample = (const u8 *)Read32(hachiemon.pcmTable1 + channel->instrument * 4);
        signedPitch = (s32)channel->note + channel->transpose;
        pitch = (u16)signedPitch;
        voice->voice = (u8)((channel->slot & 1) + 1);
        life = channel->wait;
        if (channel->envelopeEnabled) {
            if (life > channel->release)
                life -= channel->release;
            else
                life = 1;
        }
        PcmStart(voice, sample, pitch, (u8)level, (u16)life, 0);
    }
    if (voice->sample != 0) {
        channel->flags |= CH_DIRTY_ROUTE;
        WriteRoute(channel);
    }
    if (channel->mode != 1)
        ModulationSetup(channel);
    channel->flags &= (u8)~CH_DIRTY_START;
}

static void ApplyOutputs(void)
{
    HachiChannel *channel;
    u8 slot;
    for (slot = 0; slot < 4; slot++) {
        channel = PhysicalChannel(slot);
        if (channel != 0) {
            WriteRoute(channel);
            if (channel->engine == 0)
                WritePulse(channel, REG_SOUND1CNT_H, REG_SOUND1CNT_X, 1);
            else if (channel->engine == 1)
                WritePulse(channel, REG_SOUND2CNT_L, REG_SOUND2CNT_H, 0);
            else if (channel->engine == 2)
                WriteWave(channel);
            else
                WriteNoise(channel);
            WriteEcho(channel);
        }
    }
    channel = PhysicalChannel(8);
    if (channel != 0) {
        WriteRoute(channel);
        PcmApply(channel, &hachiemon.pcm[0]);
    }
    channel = PhysicalChannel(9);
    if (channel != 0) {
        WriteRoute(channel);
        PcmApply(channel, &hachiemon.pcm[1]);
    }
}

static void AdvanceEcho(void)
{
    u8 slot;
    HachiChannel *channel;
    for (slot = 0; slot < 3; slot++) {
        channel = &hachiemon.channel[slot];
        if (channel->echo != 0) {
            channel->echo->notes[channel->echo->cursor] = channel->volume != 0 ? (u8)channel->note : 0xFF;
            channel->echo->cursor++;
            channel->echo->cursor &= 47;
        }
    }
}

void HachiemonReset(void)
{
    u8 slot;
    u32 i;
    u8 *bytes;
    bytes = (u8 *)&hachiemon;
    for (i = 0; i < sizeof(HachiContext); i++)
        bytes[i] = 0;
    for (slot = 0; slot < 13; slot++) {
        HachiChannel *channel;
        channel = &hachiemon.channel[slot];
        channel->slot = slot;
        channel->engine = engineBySlot[slot];
        channel->flags = 0x5F;
        channel->sweep = 8;
        channel->note = 0x00FF;
        channel->savedVolume = 15;
        channel->echoNote = 0xFF;
        channel->gain = 0x1000;
        channel->targetGain = 0x1000;
        channel->routing = 3;
    }
    for (slot = 0; slot < 3; slot++) {
        hachiemon.channel[slot].echo = &hachiemon.echo[slot];
        for (i = 0; i < 48; i++)
            hachiemon.echo[slot].notes[i] = 0xFF;
    }
    hachiemon.pcm[0].voice = 1;
    hachiemon.pcm[1].voice = 2;
    hachiemon.masterFlags = 1;
    hachiemon.song = 2;
    REG16(REG_SOUNDCNT_L) = 0xFF77;
    REG16(REG_SOUNDCNT_H) = 0x400E;
    REG16(REG_SOUNDCNT_X) = 0x0080;
    REG16(REG_SOUNDBIAS) = 0x0200;
    REG16(REG_SOUND3CNT_L) = 0x00C0;
    for (i = 0; i < 8; i++)
        REG16(REG_WAVE_RAM + i * 2) = Read16(ROM_WAVE_TABLE + i * 2);
    REG16(REG_SOUND3CNT_L) = 0x0080;
}

void HachiemonSetSongTable(const u8 *table)
{
    hachiemon.songTable = table;
}

void HachiemonSetEffectTable(const u8 *table)
{
    hachiemon.effectTable = table;
}

void HachiemonSetPcmTable1(const u8 *table)
{
    hachiemon.pcmTable1 = table;
}

void HachiemonSetPcmTable2(const u8 *table)
{
    hachiemon.pcmTable2 = table;
}

void HachiemonSetPercussionTable(const u8 *table)
{
    hachiemon.percussionTable = table;
}

void HachiemonStopChannel(u8 slot)
{
    HachiChannel *channel;
    HachiChannel *underlying;
    HachiChannel *selected;
    if (slot >= 13)
        return;
    channel = &hachiemon.channel[slot];
    channel->stream = 0;
    channel->sweep = 0;
    channel->volume = 0;
    channel->note = 0x00FF;
    channel->flags |= 0x0A;
    channel->priority = 0;
    if (slot <= 3) {
        if (hachiemon.channel[slot + 4].stream == 0)
            ChannelWrite(channel);
    } else if (slot <= 7) {
        underlying = &hachiemon.channel[slot - 4];
        if (underlying->stream != 0) {
            underlying->flags = 0x5F;
            ChannelWrite(underlying);
        }
    } else if (slot <= 9) {
        if (hachiemon.channel[slot + 2].stream == 0)
            PcmStop(&hachiemon.pcm[slot & 1]);
    } else if (slot <= 11) {
        underlying = &hachiemon.channel[slot - 2];
        if (underlying->stream != 0) {
            underlying->flags = 0x5F;
            ChannelWrite(underlying);
        } else {
            PcmStop(&hachiemon.pcm[slot & 1]);
        }
    } else {
        selected = PhysicalChannel(3);
        if (selected != 0)
            ChannelWrite(selected);
    }
}

void HachiemonStartSong(u8 song)
{
    const u8 *header;
    const u8 *stream;
    u16 mask;
    u16 bit;
    u8 slot;
    if (hachiemon.busy || hachiemon.songTable == 0)
        return;
    hachiemon.busy = 1;
    header = (const u8 *)Read32(hachiemon.songTable + (u32)song * 4);
    mask = Read16(header);
    stream = header + 4;
    HachiemonStopChannel(0);
    HachiemonStopChannel(1);
    HachiemonStopChannel(2);
    HachiemonStopChannel(3);
    HachiemonStopChannel(8);
    HachiemonStopChannel(9);
    HachiemonStopChannel(12);
    bit = 0x0800;
    for (slot = 0; slot < 12; slot++) {
        if (mask & bit) {
            HachiChannel *channel;
            channel = &hachiemon.channel[slot];
            ChannelInit(channel, (const u8 *)Read32(stream));
            stream += 4;
            channel->priority = header[2];
        }
        bit >>= 1;
    }
    if (mask & 4) {
        HachiChannel *channel;
        channel = &hachiemon.channel[12];
        ChannelInit(channel, hachiemon.effectTable);
        channel->priority = header[2];
    }
    hachiemon.song = song;
    hachiemon.busy = 0;
}

void HachiemonStartEffect(u8 effect)
{
    const u8 *header;
    const u8 *stream;
    HachiChannel *channel;
    u16 mask;
    u16 bit;
    u8 slot;
    if (hachiemon.effectTable == 0)
        return;
    header = (const u8 *)Read32(hachiemon.effectTable + (u32)effect * 4);
    mask = Read16(header);
    stream = header + 4;
    bit = 0x0800;
    for (slot = 0; slot < 12; slot++) {
        if (mask & bit) {
            channel = &hachiemon.channel[slot];
            if (channel->priority <= header[2]) {
                HachiemonStopChannel(slot);
                ChannelInit(channel, (const u8 *)Read32(stream));
                channel->priority = header[2];
            }
            stream += 4;
        }
        bit >>= 1;
    }
}

void HachiemonSetMusicMuted(u8 muted)
{
    if (muted)
        hachiemon.masterFlags |= 2;
    else
        hachiemon.masterFlags &= (u8)~2;
}

void HachiemonUpdate(void)
{
    s16 slot;
    if (hachiemon.busy)
        return;
    hachiemon.busy = 1;
    for (slot = 12; slot >= 0; slot--) {
        if ((hachiemon.masterFlags & 2) && (slot <= 3 || (slot >= 8 && slot <= 9)))
            continue;
        ChannelTick(&hachiemon.channel[slot]);
    }
    ApplyOutputs();
    AdvanceEcho();
    hachiemon.busy = 0;
}

void HachiemonPcmAgeFrame(void)
{
    u8 i;
    for (i = 0; i < 2; i++) {
        HachiPcm *voice;
        voice = &hachiemon.pcm[i];
        if (voice->sample != 0 && (voice->flags & 1) == 0)
            voice->pendingAge++;
    }
}

void HachiemonPcmDmaComplete(u8 number)
{
    HachiPcm *voice;
    u32 length;
    u32 loopStart;
    u32 loopFlags;
    const u8 *source;
    if (number == 0 || number > 2)
        return;
    voice = &hachiemon.pcm[number - 1];
    if (voice->sample == 0 || (voice->flags & 1))
        return;
    voice->position += 16;
    voice->age = (u16)(voice->age + voice->pendingAge);
    voice->pendingAge = 0;
    length = Read32(voice->sample);
    loopFlags = Read32(voice->sample + 8);
    loopStart = Read32(voice->sample + 12);
    if (voice->position >= length - 0x20) {
        if ((loopFlags & 1) == 0) {
            PcmStop(voice);
            return;
        }
        voice->position = loopStart;
        source = voice->sample + 0x10 + loopStart;
        StartDma(voice, source, 0);
    }
    if (voice->life <= voice->age) {
        PcmStop(voice);
        return;
    }
    if (voice->flags & 2) {
        voice->rate = (u32)((s32)voice->rate + voice->rateOffset);
        voice->timer = TimerWord(voice->rate);
        if (number == 1)
            REG32(REG_TM0CNT) = voice->timer;
        else
            REG32(REG_TM1CNT) = voice->timer;
        voice->flags &= ~2UL;
    }
}

void HachiemonVolumeRamp(u8 slot, s32 step, u32 frames, u16 target)
{
    HachiChannel *channel;
    if (slot >= 13)
        return;
    channel = &hachiemon.channel[slot];
    channel->gainStep = (s16)step;
    channel->rampWait = frames;
    channel->targetGain = target;
}
