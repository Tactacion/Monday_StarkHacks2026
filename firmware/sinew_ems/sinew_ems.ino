/*
 * Sinew EMS v2.0 — 3-finger relay controller
 * D2=INDEX  D3=MIDDLE  D4=PINKY
 * Direct PORTD manipulation (~100x faster than digitalWrite)
 *
 * Protocol (115200 baud, \n terminated, auto-uppercased):
 *
 *   --- legacy (still supported) ---
 *   1/2/3          ON  index/middle/pinky
 *   4/5/6          OFF index/middle/pinky
 *   ALL            all three ON
 *   OFF            immediate all-off
 *   DANCE          built-in test sequence
 *   SEQ:f:d:f:d    sequential pattern
 *
 *   --- receiver protocol ---
 *   FINGER:INDEX:ON     single finger on
 *   FINGER:MIDDLE:OFF   single finger off
 *   ALL:OFF             immediate all-off (from /stop)
 *
 *   --- new modes ---
 *   CHORD:mask:dur       simultaneous fingers (mask 1-7, dur ms)
 *   PIANO:MARY           hardcoded song
 *   PIANO:HOTCROSS       hardcoded song
 *   PIANO:SCALE          finger exercise
 *   PIANO:TRILL          alternating exercise
 *   PIANO:ARPEGGIO       chord exercise
 *   SIGN:WORD:hold_ms    ASL fingerspelling (max 12 chars)
 *   RAPID:f:reps:on:off  rapid pulse stress test
 *   STRESS               full hardware stress test
 *
 * Safety:
 *   5-second max-on watchdog (AUTO-OFF)
 *   All long sequences are interruptible (abort on serial input)
 *   Physical kill switch is independent of software
 */

#include <avr/pgmspace.h>

#define B_IDX  0x04   // D2 bit
#define B_MID  0x08   // D3 bit
#define B_PNK  0x10   // D4 bit
#define B_ALL  (B_IDX | B_MID | B_PNK)
#define MAX_ON 5000
#define BUFSZ  64

static const byte bits[] = {B_IDX, B_MID, B_PNK};
static bool act[3];
static unsigned long tOn;
static char buf[BUFSZ];
static byte bLen;

// ── helpers ──────────────────────────────────────────────────────────

void allOff() {
    PORTD &= ~B_ALL;
    act[0] = act[1] = act[2] = false;
    tOn = 0;
    Serial.println("OFF");
}

byte maskToPort(byte mask) {
    byte p = 0;
    if (mask & 1) p |= B_IDX;
    if (mask & 2) p |= B_MID;
    if (mask & 4) p |= B_PNK;
    return p;
}

byte bitToIdx(byte b) {
    if (b == B_IDX) return 0;
    if (b == B_MID) return 1;
    return 2;
}

// Interruptible delay. Returns true if serial data arrived (caller should abort).
bool waitMs(unsigned int ms) {
    unsigned long start = millis();
    while (millis() - start < ms) {
        if (Serial.available()) return true;
    }
    return false;
}

// ── FINGER:X:ON/OFF handler ─────────────────────────────────────────

void handleFinger() {
    // buf = "FINGER:INDEX:ON" or "FINGER:MIDDLE:OFF" etc.
    char* p = buf + 7;  // skip "FINGER:"
    byte fingerBit = 0;
    if      (strncmp(p, "INDEX:",  6) == 0) { fingerBit = B_IDX; p += 6; }
    else if (strncmp(p, "MIDDLE:", 7) == 0) { fingerBit = B_MID; p += 7; }
    else if (strncmp(p, "PINKY:",  6) == 0) { fingerBit = B_PNK; p += 6; }
    else { Serial.println("ERR:FINGER"); return; }

    byte idx = bitToIdx(fingerBit);
    if (strcmp(p, "ON") == 0) {
        PORTD |= fingerBit;
        act[idx] = true;
        tOn = millis();
        Serial.println("OK");
    } else if (strcmp(p, "OFF") == 0) {
        PORTD &= ~fingerBit;
        act[idx] = false;
        Serial.println("OK");
    } else {
        Serial.println("ERR:ACTION");
    }
}

// ── DANCE ────────────────────────────────────────────────────────────

void dance() {
    Serial.println("DANCE");
    for (byte i = 0; i < 3; i++) {
        PORTD |= bits[i];
        if (waitMs(250)) { allOff(); return; }
        PORTD &= ~bits[i];
        if (waitMs(80))  { allOff(); return; }
    }
    for (int i = 2; i >= 0; i--) {
        PORTD |= bits[i];
        if (waitMs(250)) { allOff(); return; }
        PORTD &= ~bits[i];
        if (waitMs(80))  { allOff(); return; }
    }
    PORTD |= B_ALL;
    if (waitMs(400)) { allOff(); return; }
    PORTD &= ~B_ALL;
    Serial.println("DANCE:DONE");
}

// ── SEQ ──────────────────────────────────────────────────────────────

void seq() {
    Serial.println("SEQ");
    char* p = buf + 4;
    while (*p) {
        int fi = atoi(p) - 1;
        while (*p && *p != ':') p++;
        if (*p == ':') p++;
        int ms = atoi(p);
        if (ms > 2000) ms = 2000;
        while (*p && *p != ':') p++;
        if (*p == ':') p++;
        if (fi >= 0 && fi < 3 && ms > 0) {
            PORTD |= bits[fi];
            if (waitMs(ms)) { allOff(); return; }
            PORTD &= ~bits[fi];
        }
    }
    Serial.println("SEQ:DONE");
}

// ── CHORD ────────────────────────────────────────────────────────────

void chord() {
    // CHORD:mask:duration_ms  (mask 1-7)
    char* p = buf + 6;  // skip "CHORD:"
    int mask = atoi(p);
    while (*p && *p != ':') p++;
    if (*p == ':') p++;
    int dur = atoi(p);
    if (dur > 2000) dur = 2000;
    if (mask < 0 || mask > 7) { Serial.println("ERR:MASK"); return; }

    byte port = maskToPort(mask);
    Serial.println("OK");
    PORTD = (PORTD & ~B_ALL) | port;
    tOn = millis();
    if (waitMs(dur)) { allOff(); return; }
    PORTD &= ~B_ALL;
    Serial.println("CHORD:DONE");
}

// ═══════════════════════════════════════════════════════════════════════
// PIANO — hardcoded songs stored in PROGMEM
// Notes: B_IDX=E(high) B_MID=D(mid) B_PNK=C(low) 0=rest
// ═══════════════════════════════════════════════════════════════════════

// Song data: pairs of (finger_mask, beat_count).  1 beat = BEAT_MS.
// Half note = 2, quarter = 1.  finger_mask 0 = rest.  NOTE_END terminates.
#define NOTE_END     0xFF
#define BEAT_MS      600   // full quarter-note hold
#define BEAT_SHORT   300   // shortened pulse when same finger repeats next
#define NOTE_GAP_MS  120   // release gap between different fingers
#define RETRIG_MS     80   // quick clean break between same-finger repeats

// Mary Had a Little Lamb — real rhythm
// E=INDEX  D=MIDDLE  C=PINKY   q=1 beat  h=2 beats
const byte PROGMEM song_mary[] = {
    // "Ma- ry  had a  lit-tle lamb"        E  D  C  D  E  E  E~
    B_IDX,1, B_MID,1, B_PNK,1, B_MID,1, B_IDX,1, B_IDX,1, B_IDX,2,
    // "lit-tle lamb"                        D  D  D~
    B_MID,1, B_MID,1, B_MID,2,
    // "lit-tle lamb"                        E  E  E~
    B_IDX,1, B_IDX,1, B_IDX,2,
    // "Ma- ry  had a  lit-tle lamb, its"   E  D  C  D  E  E  E  E
    B_IDX,1, B_MID,1, B_PNK,1, B_MID,1, B_IDX,1, B_IDX,1, B_IDX,1, B_IDX,1,
    // "fleece was white as snow"            D  D  E  D  C~
    B_MID,1, B_MID,1, B_IDX,1, B_MID,1, B_PNK,2,
    NOTE_END
};

// Hot Cross Buns — real rhythm
const byte PROGMEM song_hotcross[] = {
    // "Hot  cross buns"                     E  D  C~~
    B_IDX,1, B_MID,1, B_PNK,2,
    // "Hot  cross buns"
    B_IDX,1, B_MID,1, B_PNK,2,
    // "One a pen-ny two a pen-ny"           C C C C  D D D D
    B_PNK,1, B_PNK,1, B_PNK,1, B_PNK,1, B_MID,1, B_MID,1, B_MID,1, B_MID,1,
    // "Hot  cross buns"
    B_IDX,1, B_MID,1, B_PNK,2,
    NOTE_END
};

// Scale exercise (up-down x3, all quarter notes)
const byte PROGMEM song_scale[] = {
    B_IDX,1, B_MID,1, B_PNK,1,  B_PNK,1, B_MID,1, B_IDX,1,
    B_IDX,1, B_MID,1, B_PNK,1,  B_PNK,1, B_MID,1, B_IDX,1,
    B_IDX,1, B_MID,1, B_PNK,1,  B_PNK,1, B_MID,1, B_IDX,1,
    NOTE_END
};

// Trill exercise (alternating pairs, quarter notes)
const byte PROGMEM song_trill[] = {
    B_IDX,1, B_MID,1, B_IDX,1, B_MID,1, B_IDX,1, B_MID,1,
    B_MID,1, B_PNK,1, B_MID,1, B_PNK,1, B_MID,1, B_PNK,1,
    B_IDX,1, B_PNK,1, B_IDX,1, B_PNK,1, B_IDX,1, B_PNK,1,
    NOTE_END
};

// Arpeggio exercise (singles → dyads → triad → down)
const byte PROGMEM song_arpeggio[] = {
    B_IDX,1, B_MID,1, B_PNK,1,                                         // singles up
    B_IDX|B_MID,1,  B_MID|B_PNK,1,  B_IDX|B_PNK,1,                    // dyads
    B_ALL,2,                                                             // full chord hold
    0,1,                                                                 // rest
    B_PNK,1, B_MID,1, B_IDX,1,                                         // singles down
    B_PNK|B_MID,1,  B_MID|B_IDX,1,  B_PNK|B_IDX,1,                    // dyads down
    B_ALL,2,                                                             // full chord hold
    NOTE_END
};

void playSong(const byte* song, const char* name) {
    Serial.print("PIANO:");
    Serial.println(name);
    byte prevNote = 0;
    for (int i = 0; ; i += 2) {
        byte note  = pgm_read_byte(&song[i]);
        if (note == NOTE_END) break;
        byte beats = pgm_read_byte(&song[i + 1]);

        // Peek ahead: is the next note the same finger?
        byte nextNote = pgm_read_byte(&song[i + 2]);  // NOTE_END if last
        bool sameAsPrev = (note && note == prevNote);
        bool sameAsNext = (nextNote != NOTE_END && note == nextNote);

        // ── gap before this note ──
        if (prevNote) {
            PORTD &= ~B_ALL;
            if (sameAsPrev) {
                // Same finger again — quick clean break so user feels re-trigger
                if (waitMs(RETRIG_MS)) { allOff(); return; }
            } else {
                // Switching fingers — longer gap for muscle transition
                if (waitMs(NOTE_GAP_MS)) { allOff(); return; }
            }
        }

        // ── hold duration ──
        // If this is a short note (1 beat) AND the next note uses the same
        // finger, shorten the pulse so the repeated taps feel crisp.
        // Final note in a repeat run or held notes get full duration.
        unsigned int hold;
        if (beats == 1 && sameAsNext) {
            hold = BEAT_SHORT;
        } else {
            hold = (unsigned int)beats * BEAT_MS;
        }

        // ── fire ──
        PORTD = (PORTD & ~B_ALL) | (note & B_ALL);
        if (note) tOn = millis();
        if (waitMs(hold)) { allOff(); return; }

        prevNote = note;
    }
    PORTD &= ~B_ALL;
    Serial.println("PIANO:DONE");
}

void piano() {
    // PIANO:MARY etc.
    char* name = buf + 6;  // skip "PIANO:"
    if      (strcmp(name, "MARY")     == 0) playSong(song_mary, "MARY");
    else if (strcmp(name, "HOTCROSS") == 0) playSong(song_hotcross, "HOTCROSS");
    else if (strcmp(name, "SCALE")    == 0) playSong(song_scale, "SCALE");
    else if (strcmp(name, "TRILL")    == 0) playSong(song_trill, "TRILL");
    else if (strcmp(name, "ARPEGGIO") == 0) playSong(song_arpeggio, "ARPEGGIO");
    else Serial.println("ERR:SONG");
}

// ═══════════════════════════════════════════════════════════════════════
// SIGN LANGUAGE — ASL 3-finger approximations
// Each letter → PORTD bitmask  (1=curled/EMS-on, 0=extended/relaxed)
// ═══════════════════════════════════════════════════════════════════════

// A-Z lookup: index is (letter - 'A')
const byte PROGMEM asl_map[26] = {
    B_ALL,                // A  fist
    0,                    // B  open hand
    B_ALL,                // C  curved ≈ fist
    B_MID | B_PNK,        // D  index points
    B_ALL,                // E  fist variant
    B_PNK,                // F  OK-ish
    B_MID | B_PNK,        // G  point sideways
    B_MID | B_PNK,        // H  like G
    B_IDX | B_MID,        // I  pinky up
    B_IDX | B_MID,        // J  like I + motion
    B_MID | B_PNK,        // K  point
    B_MID | B_PNK,        // L  L-shape
    B_ALL,                // M  fist over thumb
    B_ALL,                // N  fist over thumb
    B_ALL,                // O  tips touch
    B_MID | B_PNK,        // P  like K down
    B_MID | B_PNK,        // Q  like G down
    B_MID | B_PNK,        // R  crossed ≈ point
    B_ALL,                // S  fist
    B_ALL,                // T  fist + thumb
    B_PNK,                // U  two up
    B_PNK,                // V  peace
    0,                    // W  three up
    B_IDX,                // X  hooked index
    B_IDX | B_MID,        // Y  hang loose
    B_MID | B_PNK,        // Z  trace Z
};

void sign() {
    // SIGN:HELLO:500  (word, hold_ms per letter; hold_ms optional, default 500)
    char* p = buf + 5;  // skip "SIGN:"
    char word[13];
    byte wLen = 0;
    while (*p && *p != ':' && wLen < 12) {
        word[wLen++] = *p++;
    }
    word[wLen] = '\0';
    int hold = 500;
    if (*p == ':') hold = atoi(p + 1);
    if (hold > 2000) hold = 2000;
    if (hold < 50) hold = 50;

    Serial.print("SIGN:");
    Serial.println(word);

    for (byte i = 0; i < wLen; i++) {
        char c = word[i];
        if (c < 'A' || c > 'Z') continue;
        byte pattern = pgm_read_byte(&asl_map[c - 'A']);
        PORTD = (PORTD & ~B_ALL) | pattern;
        if (pattern) tOn = millis();
        if (waitMs(hold)) { allOff(); return; }
        PORTD &= ~B_ALL;
        if (i < wLen - 1) {
            if (waitMs(200)) { allOff(); return; }  // gap between letters
        }
    }
    PORTD &= ~B_ALL;
    Serial.println("SIGN:DONE");
}

// ═══════════════════════════════════════════════════════════════════════
// RAPID — pulse a single finger rapidly for stress testing
// RAPID:finger:reps:on_ms:off_ms
// ═══════════════════════════════════════════════════════════════════════

void rapid() {
    char* p = buf + 6;  // skip "RAPID:"
    int fi = atoi(p) - 1;
    while (*p && *p != ':') p++;  if (*p == ':') p++;
    int reps = atoi(p);
    while (*p && *p != ':') p++;  if (*p == ':') p++;
    int onMs = atoi(p);
    while (*p && *p != ':') p++;  if (*p == ':') p++;
    int offMs = atoi(p);

    if (fi < 0 || fi > 2) { Serial.println("ERR:FINGER"); return; }
    if (reps > 100) reps = 100;
    if (onMs > 1000) onMs = 1000;
    if (offMs > 1000) offMs = 1000;

    Serial.println("RAPID");
    for (int r = 0; r < reps; r++) {
        PORTD |= bits[fi];
        tOn = millis();
        if (waitMs(onMs)) { allOff(); return; }
        PORTD &= ~bits[fi];
        if (waitMs(offMs)) { allOff(); return; }
    }
    Serial.println("RAPID:DONE");
}

// ═══════════════════════════════════════════════════════════════════════
// STRESS — full hardware stress test
// Phase 1: rapid individual (10ms)
// Phase 2: all 7 combinations (50ms each)
// Phase 3: high-speed multiplex (15ms)
// Phase 4: sustained holds (500ms)
// ═══════════════════════════════════════════════════════════════════════

void stress() {
    Serial.println("STRESS");

    // Phase 1: rapid individual switches
    for (byte rep = 0; rep < 5; rep++) {
        for (byte i = 0; i < 3; i++) {
            PORTD |= bits[i];
            if (waitMs(10)) { allOff(); return; }
            PORTD &= ~bits[i];
            if (waitMs(5))  { allOff(); return; }
        }
    }

    // Phase 2: all 7 finger combinations
    for (byte mask = 1; mask <= 7; mask++) {
        PORTD = (PORTD & ~B_ALL) | maskToPort(mask);
        if (waitMs(50))  { allOff(); return; }
        PORTD &= ~B_ALL;
        if (waitMs(20))  { allOff(); return; }
    }

    // Phase 3: high-speed multiplex (simulates grab at max speed)
    for (byte rep = 0; rep < 20; rep++) {
        for (byte i = 0; i < 3; i++) {
            PORTD |= bits[i];
            if (waitMs(15)) { allOff(); return; }
            PORTD &= ~bits[i];
        }
    }

    // Phase 4: sustained holds per finger then all
    for (byte i = 0; i < 3; i++) {
        PORTD |= bits[i];
        tOn = millis();
        if (waitMs(500)) { allOff(); return; }
        PORTD &= ~bits[i];
        if (waitMs(100)) { allOff(); return; }
    }
    PORTD |= B_ALL;
    tOn = millis();
    if (waitMs(500)) { allOff(); return; }

    allOff();
    Serial.println("STRESS:DONE");
}

// ═══════════════════════════════════════════════════════════════════════
// SETUP + LOOP
// ═══════════════════════════════════════════════════════════════════════

void setup() {
    DDRD |= B_ALL;
    PORTD &= ~B_ALL;
    Serial.begin(115200);
    Serial.println("READY");
}

void loop() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') {
            buf[bLen] = '\0';

            // --- Legacy single-char commands ---
            if (bLen == 1 && buf[0] >= '1' && buf[0] <= '3') {
                byte i = buf[0] - '1';
                PORTD |= bits[i]; act[i] = true; tOn = millis();
                Serial.println("OK");
            }
            else if (bLen == 1 && buf[0] >= '4' && buf[0] <= '6') {
                byte i = buf[0] - '4';
                PORTD &= ~bits[i]; act[i] = false;
                Serial.println("OK");
            }
            // --- 3-char: ALL / OFF ---
            else if (bLen == 3 && buf[0] == 'A' && buf[1] == 'L' && buf[2] == 'L') {
                PORTD |= B_ALL; act[0] = act[1] = act[2] = true; tOn = millis();
                Serial.println("OK");
            }
            else if (bLen == 3 && buf[0] == 'O' && buf[1] == 'F' && buf[2] == 'F') {
                allOff();
            }
            // --- ALL:OFF (receiver /stop) ---
            else if (bLen == 7 && strncmp(buf, "ALL:OFF", 7) == 0) {
                allOff();
            }
            // --- FINGER:X:ON/OFF (receiver /stimulate) ---
            else if (bLen > 10 && buf[0] == 'F' && buf[1] == 'I' && buf[6] == ':') {
                handleFinger();
            }
            // --- DANCE ---
            else if (bLen == 5 && buf[0] == 'D' && buf[1] == 'A') {
                dance();
            }
            // --- SEQ:... ---
            else if (bLen > 4 && buf[0] == 'S' && buf[1] == 'E' && buf[2] == 'Q') {
                seq();
            }
            // --- CHORD:mask:dur ---
            else if (bLen > 5 && buf[0] == 'C' && buf[1] == 'H') {
                chord();
            }
            // --- PIANO:song ---
            else if (bLen > 5 && buf[0] == 'P' && buf[1] == 'I') {
                piano();
            }
            // --- SIGN:word:hold ---
            else if (bLen > 4 && buf[0] == 'S' && buf[1] == 'I') {
                sign();
            }
            // --- RAPID:f:n:on:off ---
            else if (bLen > 5 && buf[0] == 'R' && buf[1] == 'A') {
                rapid();
            }
            // --- STRESS ---
            else if (bLen == 6 && buf[0] == 'S' && buf[1] == 'T') {
                stress();
            }

            bLen = 0;
        } else if (c != '\r' && bLen < BUFSZ - 1) {
            buf[bLen++] = (c >= 'a' && c <= 'z') ? c - 32 : c;
        }
    }

    // Watchdog: 5-second max-on cap
    if (tOn && (millis() - tOn > MAX_ON)) {
        Serial.println("AUTO-OFF");
        allOff();
    }
}
