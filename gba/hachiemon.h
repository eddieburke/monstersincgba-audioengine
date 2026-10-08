#ifndef HACHIEMON_H
#define HACHIEMON_H

typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef signed short s16;
typedef unsigned int u32;
typedef signed int s32;

typedef struct HachiChannel HachiChannel;
typedef struct HachiEcho HachiEcho;
typedef struct HachiPcm HachiPcm;
typedef struct HachiContext HachiContext;

struct HachiChannel {
    u32 field00;
    HachiEcho *echo;
    u32 field08;
    const u8 *stream;
    const u8 *loop1;
    const u8 *loop2;
    const u8 *returnPtr;
    const u8 *macro;
    u32 wait;
    u32 baseDuration;
    u32 macroWait;
    s32 modulationDelta;
    s32 modulation;
    u32 rampWait;
    u16 gain;
    u16 targetGain;
    s16 gainStep;
    u16 frequency;
    u16 note;
    u16 levelProduct;
    u8 flags;
    u8 slot;
    u8 loop1Count;
    u8 loop2Count;
    u8 sweep;
    u8 offset;
    u8 modulationDelay;
    u8 modulationPeriod;
    u8 modulationDepth;
    u8 modulationDelayLeft;
    u8 modulationPeriodLeft;
    u8 attack;
    u8 decay;
    u8 releaseWindow;
    u8 release;
    u8 envelopeCount;
    u8 envelopeStep;
    u8 envelopeState;
    u8 envelopeEnabled;
    u8 mode;
    u8 engine;
    u8 savedVolume;
    u8 volume;
    u8 instrument;
    u8 field5c;
    u8 parameter;
    s8 transpose;
    u8 echoDelay;
    u8 echoNote;
    u8 priority;
    u8 durationHigh;
    u8 routing;
};

struct HachiEcho {
    u8 notes[48];
    u8 cursor;
    u8 unused[3];
};

struct HachiPcm {
    const u8 *sample;
    u32 rate;
    u32 position;
    u32 flags;
    u32 timer;
    s32 rateOffset;
    u16 life;
    u16 age;
    u16 pendingAge;
    u8 voice;
    u8 pitch;
    u8 volume;
    u8 unused21[3];
};

struct HachiContext {
    HachiChannel channel[13];
    HachiEcho echo[3];
    const u8 *songTable;
    const u8 *effectTable;
    HachiPcm pcm[2];
    const u8 *pcmTable1;
    const u8 *pcmTable2;
    const u8 *percussionTable;
    u8 masterFlags;
    u8 unused60d[2];
    u8 busy;
    u8 song;
};

extern HachiContext hachiemon;

void HachiemonReset(void);
void HachiemonSetSongTable(const u8 *table);
void HachiemonSetEffectTable(const u8 *table);
void HachiemonSetPcmTable1(const u8 *table);
void HachiemonSetPcmTable2(const u8 *table);
void HachiemonSetPercussionTable(const u8 *table);
void HachiemonStartSong(u8 song);
void HachiemonStartEffect(u8 effect);
void HachiemonStopChannel(u8 slot);
void HachiemonSetMusicMuted(u8 muted);
void HachiemonUpdate(void);
void HachiemonPcmAgeFrame(void);
void HachiemonPcmDmaComplete(u8 voice);
void HachiemonVolumeRamp(u8 slot, s32 step, u32 frames, u16 target);

#endif
