// Easter egg in the header's top-right corner: Clawd, the Claude Code banner
// mascot, living its day on the local clock: asleep, coffee, coding, lunch, a
// pastime per half hour from 5pm (a run, a ride, bass, a beer with friends,
// the kids, …), dinner, a book; weekends sleep in, have brunch and take
// pastimes from 11am (tamagotchi.js has the schedule; TamaPastimes.jsx draws
// the pastimes, TamaDishes.jsx the dishes). Double-click to pet it.
//
// It's also on duty (clawdDuty.js decides what, TamaDuty.jsx draws it): one
// thing at a time, highest priority first, with a bubble counting what's
// waiting — and a click goes straight to it. Petting stays a single click
// when nothing is pending.
//
// Coding happens at a desk, reading in an armchair by the fireplace; each
// pastime's pose, props and setting (the outdoor ones in the current season)
// are its record in TamaPastimes.PASTIME_SCENES.
//
// Pure pixel art: a 34×24 scene where each sprite cell is 1×2 (the banner's
// terminal quadrants are twice as tall as wide), drawn with crisp-edged
// rects. All motion is CSS (styles.css, `.tama`) and switches off under
// prefers-reduced-motion.
import { computed, useComputed, useSignal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { accountLabel, multiAccount } from "../accounts.js";
import { getClawdDay, getTamaCritter, noteClawdDay } from "../prefs.js";
import { selectedPid } from "../split/alertFeed.js";
import { buildRunning } from "../split/attention.js";
import {
  alerts, dismissedAlertIds, dismissedNeedsPids, dismissedReadyPids, railSelection, updateInfo, usage, wallSpans, walls,
  windows,
} from "../store.js";
import { plural } from "../util.js";
import { Critter } from "./Critter.jsx";
import { clickHandlers } from "./clawdClick.js";
import { deskView, dutyItems, moreCount, wallLabel, wallPhrase } from "./clawdDuty.js";
import { mergeParties, startClawdWatch } from "./clawdWatch.js";
import {
  CLAWD_HEIGHT, CRITTER_HEIGHT, CRITTERS, critterById, hatTransform, holdTipInScene, placement, r4, sceneToSprite,
} from "./critters.js";
import { Dish } from "./TamaDishes.jsx";
import {
  AlarmClock, ApiScreen, ArmsUp, BELL_REACH, Bubble, CiScreen, CtxScreen, DeskZs, DoneScreen, Extinguisher,
  HappyEyes, KEYS_REACH, LookDown, MergeScreen, PanesScreen, PaperStack, Parcel, ServiceBell, SlapHand, SlapPaw,
  Sparkles, Stars, Startle, TypingPaws, WallScreen, Worried, XEyes,
} from "./TamaDuty.jsx";
import {
  BASS_FRET, Bass, CoffeeMug, Controller, CritterHeld, Goggles, PASTIME_SCENES, Scarf, Spoon, Sprite,
} from "./TamaPastimes.jsx";
import {
  activityAt, activityLabel, BODY, clockLabel, codingMood, dayFlags, dayKey, EYE, EYES, gridCells, headlineAt,
  hourLabel, LEGS, MOODS, moodReason, msUntilNextChange, noteKey, occasionShown, SKIN, sceneKey, seasonAt, upNext,
  walledAt, Y0,
} from "./tamagotchi.js";

// Where the mascot stands per activity: centred on its feet by default; the
// sitting scenes shift it left to make room for the desk / fireplace, and
// tuck its legs out of sight. The pastimes' poses are in their own records
// (TamaPastimes.PASTIME_SCENES).
const POSES = { code: { x0: 3, sitting: true }, nap: { x0: 3, sitting: true }, read: { x0: 4, sitting: true } };
const STANDING = { x0: 8 };
const BODY_CELLS = gridCells(BODY);
// Armless at the console (`p?.paws`): row 2's side-arm cells (x 1–2 and 15–16)
// cleared back to row 0/1/3's silhouette, both hands on the controller instead.
const PAWS_BODY_CELLS = gridCells(BODY.map((row, y) => (y === 2 ? row.replace(/^.##/, "...").replace(/##$/, "..") : row)));
const LEG_CELLS = LEGS.map((frame) => gridCells([frame]));
const LEGS_Y0 = Y0 + 2 * BODY.length;

// Clawd on its own, standing, for the Settings picker (overlays/TamaPicker.jsx).
export function ClawdSprite() {
  return (
    <g>
      <Sprite cells={BODY_CELLS} cls="tama-body" x0={0} />
      <Eyes x0={0} />
      <Sprite cells={LEG_CELLS[0]} cls="tama-legs-a" x0={0} y0={LEGS_Y0} />
    </g>
  );
}

// A pastime's held prop is a scene prop, except the ones Clawd wears on his own
// face and neck: on a critter they'd float across its body, so it wears its
// own instead (critters.js wornGlyphs, via the `worn` computed below).
const WORN_ON_CLAWD = new Set([Goggles, Scarf]);

// What a critter holds for each activity, in its own appendage (critters.js
// limbGlyphs, TamaPastimes CritterHeld): Clawd's held props sit at his hand and
// would float beside a critter. The bass and the controller are held in paws
// on Clawd's own drawings instead (TamaPastimes Paw); the bass's neck is
// fretted by the limb, reaching a fixed spot on the bass (`fret` below).
const HOLDING = { beer: "pint", coffee: "mug", cook: "spoon", garden: "can", dog: "leash", music: "bass", game: "controller" };

// Props drawn on Clawd's head (top at Y0) move with a critter's head instead:
// a seated critter's head is higher than seated Clawd's. A wrapper, not a
// transform on the prop, which may animate its own.
function Head({ dy, children }) {
  return dy ? <g transform={`translate(0 ${dy})`}>{children}</g> : children;
}

function Eyes({ closed, x0 }) {
  return (
    <g class={closed ? "tama-eyes-closed" : "tama-eyes"}>
      {EYES.map((x) => (
        <rect key={x} x={x0 + x} y={closed ? Y0 + 3.3 : Y0 + 2} width="1" height={closed ? 0.5 : 2} fill={EYE} />
      ))}
    </g>
  );
}

// The run happens outdoors, in whatever season it is: sky tint, ground, a tree
// dressed for the season, and something falling (petals, leaves, snow).
const SEASONS = {
  spring: { sky: "#7fb3e0", ground: "#5c9a4c", canopy: ["#6fae5a", "#f2a7c3"], fall: "#f4b6cc" },
  summer: { sky: "#6fb4ea", ground: "#4f9443", canopy: ["#4f9a46", "#3f8238"] },
  autumn: { sky: "#c9a36b", ground: "#7a6a3a", canopy: ["#d9822b", "#b5452a"], fall: "#e07b39" },
  winter: { sky: "#9fb3c8", ground: "#e6edf3", fall: "#f4f8fb" },
};
const FALLING = [[3, 0], [11, -0.7], [18, -1.6], [25.5, -0.3], [32, -1.1]];

function Scenery({ season, night }) {
  const sc = SEASONS[season] || SEASONS.summer;
  return (
    <g class="tama-scenery" data-season={season}>
      <rect x="0" y="0" width="34" height="19.2" fill={sc.sky} opacity="0.13" />
      <rect x="0" y="19.4" width="34" height="4.6" fill={sc.ground} opacity={season === "winter" ? 0.85 : 0.55} />
      {night && (
        <g>
          <rect x="0" y="0" width="34" height="24" fill="#0b0a1a" opacity="0.5" />
          <g class="tama-moon">
            <circle cx="5" cy="4.2" r="2" fill="#f3e9c6" />
            <circle cx="4.3" cy="3.7" r="0.4" fill="#d8ccaa" />
            <circle cx="5.7" cy="4.9" r="0.3" fill="#d8ccaa" />
          </g>
          <g transform="translate(19 3.2)">
            <path class="tama-bat" fill="#1a1626"
                  d="M0 0.6 q1 -1 2 0 q0.4 -0.8 1 -0.2 q0.6 -0.6 1 0.2 q1 -1 2 0 q-1.2 0.1 -2 1 q-0.6 -0.4 -1 0 q-0.4 -0.4 -1 0 q-0.8 -0.9 -2 -1z" />
          </g>
        </g>
      )}
      {season === "summer" && !night && <circle class="tama-sun" cx="4.5" cy="4" r="2.1" fill="#ffd166" />}
      <rect x="30.2" y="11.6" width="1.4" height="7.6" fill="#6b4a33" />
      {sc.canopy ? (
        <g>
          <rect x="27.4" y="7.4" width="6.6" height="4.6" rx="1.4" fill={sc.canopy[0]} />
          <rect x="28.4" y="5.8" width="4.6" height="2.2" rx="1" fill={sc.canopy[0]} />
          <g fill={sc.canopy[1]}>
            <rect x="28.2" y="8.2" width="0.8" height="0.8" />
            <rect x="31.4" y="7" width="0.8" height="0.8" />
            <rect x="32.4" y="9.8" width="0.8" height="0.8" />
            <rect x="29.6" y="10.4" width="0.8" height="0.8" />
          </g>
        </g>
      ) : (
        <g stroke="#6b4a33" stroke-width="0.5">
          <line x1="30.9" y1="12" x2="28.4" y2="8.2" />
          <line x1="30.9" y1="11" x2="33.4" y2="8" />
          <line x1="30.9" y1="9.6" x2="30.4" y2="6.2" />
        </g>
      )}
      {season === "spring" && (
        <g>
          <rect x="3" y="19.8" width="0.7" height="0.7" fill="#f2a7c3" />
          <rect x="6.5" y="20.6" width="0.7" height="0.7" fill="#ffd166" />
          <rect x="26" y="20.2" width="0.7" height="0.7" fill="#f2a7c3" />
        </g>
      )}
      {season === "summer" && (
        <g fill="#3f8238">
          <rect x="2.6" y="18.4" width="0.4" height="1.2" />
          <rect x="3.3" y="18.1" width="0.4" height="1.5" />
          <rect x="26.4" y="18.3" width="0.4" height="1.3" />
        </g>
      )}
      {season === "autumn" && (
        <g fill="#c2452d">
          <rect x="4" y="19.7" width="1" height="0.5" />
          <rect x="24.8" y="20.1" width="1" height="0.5" />
          <rect x="27.6" y="19.6" width="1" height="0.5" fill="#e07b39" />
        </g>
      )}
      {sc.fall && (
        <g fill={sc.fall}>
          {FALLING.map(([x, delay]) => (
            <rect key={x} class="tama-fall" x={x} y="-1" width="0.7" height="0.7"
                  style={{ animationDelay: `${delay}s` }} />
          ))}
        </g>
      )}
    </g>
  );
}

// Occasions (tamagotchi.OCCASIONS), from Faraday's standup bot: Halloween
// evenings (activityAt's `trick`) send the run and the kids out in sheets, and
// the week lights a jack-o'-lantern everywhere else; the year-end wind-down
// brings a Santa hat, a tree and stockings; the new year a party hat,
// fireworks and confetti.
function JackOLantern({ x, y }) {
  return (
    <g class="tama-jack" shape-rendering="geometricPrecision">
      <rect x={x + 1.3} y={y} width="0.5" height="0.7" fill="#4f7a2c" />
      <rect x={x} y={y + 0.4} width="3.1" height="2.2" rx="1" fill="#ef8a2c" />
      <rect x={x + 1} y={y + 0.5} width="0.15" height="2" fill="#d9741f" />
      <rect x={x + 1.95} y={y + 0.5} width="0.15" height="2" fill="#d9741f" />
      <path class="tama-jack-face" fill="#ffcf5a"
            d={`M${x + 0.6} ${y + 1.4} l0.4 -0.6 l0.4 0.6z M${x + 2.1} ${y + 1.4} l0.4 -0.6 l0.4 0.6z M${x + 0.6} ${y + 1.8} h1.9 l-0.3 0.5 h-1.3z`} />
    </g>
  );
}

// Where the jack-o'-lantern sits outside the pastimes (theirs are in
// PASTIME_SCENES): on the floor, under the desk, on the mantel.
const JACK_SPOTS = {
  sleep: { x: 1.5, y: 16.4 },
  coffee: { x: 1.5, y: 16.4 },
  eat: { x: 1.5, y: 16.4 },
  code: { x: 25.5, y: 16.4 },
  nap: { x: 25.5, y: 16.4 },
  read: { x: 24.6, y: 3.4 },
};

function ChristmasTree() {
  return (
    <g class="tama-xmas-tree" shape-rendering="geometricPrecision">
      <rect x="2.9" y="18.2" width="1" height="0.8" fill="#5a3a26" />
      <polygon points="3.4,12 7.6,18.2 -0.8,18.2" fill="#276b3e" />
      <polygon points="3.4,8.6 7.2,14.6 -0.4,14.6" fill="#2f7d4a" />
      <polygon points="3.4,5.2 6.8,10.8 0,10.8" fill="#276b3e" />
      <polygon points="3.4,2.4 6.2,7.2 0.6,7.2" fill="#2f7d4a" />
      <polygon class="tama-twinkle" points="3.4,0.8 3.8,1.8 4.8,1.9 4,2.5 4.3,3.4 3.4,2.9 2.5,3.4 2.8,2.5 2,1.9 3,1.8" fill="#ffd166" />
      <circle cx="2.6" cy="6.2" r="0.42" fill="#e05252" />
      <circle class="tama-twinkle" cx="4.6" cy="9.4" r="0.42" fill="#7fb6e6" />
      <circle cx="1.4" cy="10" r="0.42" fill="#ffd166" />
      <circle class="tama-twinkle tama-twinkle2" cx="5.6" cy="12.8" r="0.42" fill="#e05252" />
      <circle cx="0.8" cy="13.8" r="0.42" fill="#c792ea" />
      <circle cx="6.6" cy="16.6" r="0.42" fill="#ffd166" />
    </g>
  );
}

function Stockings() {
  return (
    <g class="tama-stocking">
      {[25.4, 30.2].map((x) => (
        <g key={x}>
          <rect x={x} y="7.1" width="1.2" height="2.1" fill="#c0392b" />
          <rect x={x} y="8.6" width="2" height="0.9" rx="0.4" fill="#c0392b" />
          <rect x={x - 0.1} y="7.1" width="1.4" height="0.6" fill="#f4f4f6" />
        </g>
      ))}
    </g>
  );
}

function SantaHat({ x0 }) {
  return (
    <g class="tama-hat-santa">
      <g fill="#d03a3a">
        <rect x={x0 + 4} y="6.8" width="10" height="1.5" />
        <rect x={x0 + 5.6} y="5.6" width="6.6" height="1.3" />
        <rect x={x0 + 7.4} y="4.6" width="3.6" height="1.1" />
        <rect x={x0 + 10.6} y="4.6" width="3" height="1" />
        <rect x={x0 + 13} y="5.2" width="1.2" height="1" />
      </g>
      <rect x={x0 + 3} y="8.2" width="12" height="1.3" rx="0.5" fill="#f4f4f6" />
      <rect x={x0 + 13.6} y="5.6" width="1.6" height="1.6" rx="0.8" fill="#f4f4f6" />
    </g>
  );
}

function PartyHat({ x0 }) {
  return (
    <g class="tama-hat-party" shape-rendering="geometricPrecision">
      <polygon points={`${x0 + 6.6},9 ${x0 + 11.4},9 ${x0 + 9},3.8`} fill="#5aa9e6" />
      <rect x={x0 + 7.5} y="6.8" width="3" height="0.6" fill="#ffd166" />
      <rect x={x0 + 8.25} y="5.2" width="1.5" height="0.5" fill="#ffd166" />
      <circle cx={x0 + 9} cy="3.6" r="0.8" fill="#f28ab2" />
    </g>
  );
}

// A critter's hats: Clawd's own, drawn inside its group and fitted to its head
// (critters.js hatTransform) rather than on Clawd's spot. Which hat shows
// follows Clawd's rules (TamaScene). The wrapper carries the view-box pivot
// for the same reason as the critter's own: `.tama g` pivots on its fill-box.
// The seat draws inside the critter's own wrapper, ahead of the siblings that
// follow it in the creature group (api's Stars, a held prop); Clawd's hat
// draws after those instead. So those paint over a critter's hat, under
// Clawd's — cosmetic, nothing both holds a prop and wears a hat at once.
function CritterHats({ sprite, p, hat, season, ghost }) {
  const headwear = p?.Headwear && !hat;
  const snowcap = p?.snowcap && season === "winter" && !hat && !ghost;
  if (!headwear && !snowcap && !hat) return null;
  return (
    <g class="tama-hat-seat" style="transform-box: view-box" transform={hatTransform(sprite)}>
      {headwear && <p.Headwear x0={0} />}
      {snowcap && <rect class="tama-snowcap" x="3" y="8.4" width="12" height="0.7" rx="0.3" fill="#f4f8fb" />}
      {hat === "santa" && <SantaHat x0={0} />}
      {hat === "party" && <PartyHat x0={0} />}
    </g>
  );
}

function GhostSheet({ x0 }) {
  const L = x0 + 2.2;
  const R = x0 + 15.8;
  const hem = "q-0.85 1.1 -1.7 0 ".repeat(8);
  return (
    <g class="tama-ghost" shape-rendering="geometricPrecision">
      <path fill="#f5f5f7" d={`M${L} 17.6 V10.4 Q${L} 7.6 ${(L + R) / 2} 7.6 Q${R} 7.6 ${R} 10.4 V17.6 ${hem}Z`} />
      <ellipse cx={x0 + 5.6} cy="11.6" rx="0.7" ry="1" fill={EYE} />
      <ellipse cx={x0 + 12.4} cy="11.6" rx="0.7" ry="1" fill={EYE} />
      <ellipse cx={x0 + 9} cy="14" rx="0.6" ry="0.8" fill={EYE} />
    </g>
  );
}

function Pail({ x0 }) {
  const x = x0 + 16.4;
  return (
    <g class="tama-pail" shape-rendering="geometricPrecision">
      <path d={`M${x + 0.3} 13.8 Q${x + 1.8} 11.2 ${x + 3.3} 13.8`} fill="none" stroke="#3a2a1a" stroke-width="0.35" />
      <rect x={x + 0.9} y="13.1" width="0.7" height="0.7" fill="#e05252" />
      <rect x={x + 1.9} y="12.9" width="0.7" height="0.7" fill="#7fb6e6" />
      <rect x={x} y="13.6" width="3.6" height="3.2" rx="1.2" fill="#ef8a2c" />
      <path fill="#3a1e0e" d={`M${x + 0.7} 14.9 l0.5 -0.7 l0.5 0.7z M${x + 2} 14.9 l0.5 -0.7 l0.5 0.7z M${x + 0.7} 15.5 h2.2 l-0.4 0.6 h-1.4z`} />
    </g>
  );
}

// With a bubble in the top-left corner, three bursts over the desk instead.
const BURSTS = [[5, 4, "#ffd166", 0], [22, 2.6, "#f28ab2", -0.9]];
const BURSTS_CLEAR = [[17.4, 2.8, "#ffd166", 0], [24.6, 2.1, "#f28ab2", -0.6], [31.2, 3, "#7fb6e6", -1.2]];

function Fireworks({ clear }) {
  const burst = (cx, cy, color, delay) => (
    <g key={cx} class="tama-firework" style={{ animationDelay: `${delay}s` }} fill={color}>
      {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => {
        const a = (deg * Math.PI) / 180;
        return <rect key={deg} x={cx + Math.cos(a) * 1.8 - 0.25} y={cy + Math.sin(a) * 1.8 - 0.25} width="0.5" height="0.5" />;
      })}
      <rect x={cx - 0.25} y={cy - 0.25} width="0.5" height="0.5" />
    </g>
  );
  return <g>{(clear ? BURSTS_CLEAR : BURSTS).map(([cx, cy, color, delay]) => burst(cx, cy, color, delay))}</g>;
}

const CONFETTI = ["#ffd166", "#f28ab2", "#7fb6e6", "#9be38f", "#c792ea"];

function Confetti() {
  return (
    <g class="tama-confetti">
      {[[2.5, 0], [9, -0.6], [15, -1.4], [21, -0.3], [27, -1.9], [32, -1]].map(([x, delay], i) => (
        <rect key={x} class="tama-fall" x={x} y="-1" width="0.6" height="0.6" fill={CONFETTI[i % CONFETTI.length]}
              style={{ animationDelay: `${delay}s` }} />
      ))}
    </g>
  );
}

// What sits beside (behind) and in front of the mascot for each activity.
// The coding monitor's lines, by mood (tamagotchi.MOODS): grumpy turns one
// line red; furious fills the screen with errors. Drawn inside the desk, under
// the morning mug, so they rattle with the monitor and never cover the mug.
function CodeScreen({ mood }) {
  if (mood === "furious") {
    return (
      <g class="tama-errors" fill="#e05252">
        <rect x="24" y="7" width="4.4" height="0.6" />
        <rect x="24" y="8.4" width="6.2" height="0.6" />
        <rect x="24.8" y="9.8" width="3.6" height="0.6" />
        <rect x="29.4" y="9.8" width="0.6" height="0.6" fill="#ffd166" />
      </g>
    );
  }
  return (
    <g>
      <rect x="24" y="7" width="4.4" height="0.6" fill="var(--accent)" />
      {mood === "grumpy"
        ? <rect class="tama-red-line" x="24" y="8.4" width="3" height="0.6" fill="#e05252" />
        : <rect x="24" y="8.4" width="3" height="0.6" fill="#7fbf7f" />}
      <rect x="24.8" y="9.8" width="3.4" height="0.6" fill="#c792ea" />
      <rect class="tama-cursor" x="28.6" y="9.8" width="0.6" height="0.6" fill="#e6e9ef" />
    </g>
  );
}

function Behind({
  activity, occasion, coffee, ringing, keyboard = true, mood = "calm", screen = null, dish, mugHeld = false,
}) {
  switch (activity) {
    case "sleep":
      if (ringing) return null;
      return (
        <g fill="var(--fg-2)" font-family="var(--mono)" font-weight="700">
          <text class="tama-z" x="26" y="10" font-size="4">z</text>
          <text class="tama-z tama-z2" x="29" y="7" font-size="5">Z</text>
        </g>
      );
    case "coffee":
      // A critter holding the mug (CritterHeld) leaves no mug standing here.
      return <g>{!mugHeld && <CoffeeMug />}</g>;
    case "code":
      return (
        <g>
          <g class="tama-chair" fill="#4c525c">
            <rect x="1.6" y="9.6" width="1.6" height="8.4" rx="0.5" />
            <rect x="3.2" y="16.9" width="15" height="1.3" rx="0.4" />
            <rect x="10" y="18.2" width="1.2" height="0.8" />
            <rect x="7" y="18.6" width="7.2" height="0.5" rx="0.25" />
          </g>
          <g class="tama-desk">
            <rect x="20.2" y="13.2" width="13.6" height="0.9" fill="#8b6a4e" />
            <rect x="21" y="14.1" width="0.9" height="4.9" fill="#6f533c" />
            <rect x="32.2" y="14.1" width="0.9" height="4.9" fill="#6f533c" />
            <rect x="26.9" y="12" width="1.2" height="1.2" fill="var(--fg-3)" />
            <rect x="23" y="5.6" width="9" height="6.4" rx="0.4" fill={mood === "furious" ? "#2a1416" : "#1d2026"}
                  stroke="var(--fg-3)" stroke-width="0.3" />
            {screen || <CodeScreen mood={mood} />}
            {keyboard && <rect x="20.4" y="12.6" width="4.2" height="0.6" rx="0.2" fill="#c9ced6" />}
          </g>
          {coffee && (
            // The morning's mug, between the keyboard and the monitor stand.
            <g class="tama-desk-mug">
              <rect class="tama-steam" x="25.2" y="9.4" width="0.4" height="1.4" fill="var(--fg-3)" />
              <rect class="tama-steam tama-steam2" x="25.9" y="8.8" width="0.4" height="1.4" fill="var(--fg-3)" />
              <g fill="#efe6d8">
                <rect x="24.9" y="11.3" width="1.5" height="1.9" rx="0.25" />
                <rect x="26.4" y="11.8" width="0.5" height="0.35" />
                <rect x="26.6" y="11.8" width="0.3" height="1" />
                <rect x="26.4" y="12.5" width="0.5" height="0.35" />
              </g>
              <rect x="24.9" y="11.3" width="1.5" height="0.45" fill="#7a4e2d" />
            </g>
          )}
        </g>
      );
    case "read":
      return (
        <g>
          <ellipse class="tama-glow" cx="28" cy="19" rx="9" ry="2.4" fill="#f0a24a" opacity="0.18" />
          <g class="tama-fire">
            <rect x="23.4" y="6" width="10.6" height="1.1" fill="#8a7a6a" />
            <rect x="24.2" y="7.1" width="9.8" height="11.9" fill="#7b4a3a" />
            <g fill="#6a3e30">
              <rect x="24.2" y="9.4" width="9.8" height="0.3" />
              <rect x="24.2" y="11.8" width="9.8" height="0.3" />
              <rect x="24.2" y="14.2" width="9.8" height="0.3" />
              <rect x="24.2" y="16.6" width="9.8" height="0.3" />
            </g>
            <rect x="25.8" y="11" width="6.6" height="8" rx="2.4" fill="#1a1110" />
            <path class="tama-flame" d="M27.2 19 q-0.6 -2.6 1 -4.4 q0.2 1.6 1 2.2 q0.4 -1.4 1.4 -2.4 q1 2.6 0.2 4.6z" fill="#f08a3c" />
            <path class="tama-flame tama-flame2" d="M28 19 q-0.2 -1.6 0.9 -2.8 q0.3 1.1 0.9 1.4 q0.5 -0.8 0.6 -1.4 q0.9 1.6 0.2 2.8z" fill="#ffd166" />
            <rect x="26.6" y="18.4" width="5" height="0.6" rx="0.3" fill="#5a3a26" />
          </g>
          {occasion === "yearend" && <Stockings />}
          {occasion === "yearend" && <ChristmasTree />}
          <g class="tama-chair" fill="#8e3b3b">
            <rect x="1" y="8.6" width="3.2" height="9.6" rx="1" />
            <rect x="3.6" y="16.8" width="16.8" height="1.5" rx="0.6" />
          </g>
          <g class="tama-chair-legs" fill="#4a2f22">
            <rect x="1.5" y="18.2" width="0.9" height="0.8" />
            <rect x="5" y="18.2" width="0.9" height="0.8" />
            <rect x="16.4" y="18.2" width="0.9" height="0.8" />
          </g>
        </g>
      );
    case "eat":
      return <Dish dish={dish} />;
    default:
      return null;
  }
}

// A pastime's backdrop (PASTIME_SCENES): the outdoor ones in the season's
// scenery, at night on a Halloween round. Scenery is drawn here, not in
// TamaPastimes.jsx: importing it back from there would make an import cycle.
// The critter goes to the friend and the kids (TamaPastimes.jsx), which match
// the mascot; `from` is a critter's tip, for the leash's other end (Dog).
function PastimeBehind({ p, season, trick, critter, from }) {
  const drawn = <p.Behind season={season} trick={trick} critter={critter} from={from} />;
  return p.outdoors ? <g><Scenery season={season} night={trick} />{drawn}</g> : drawn;
}

function InFront({ activity }) {
  if (activity !== "read") return null;
  return (
    <g>
      <rect class="tama-chair" x="17.4" y="13.6" width="3.6" height="4.7" rx="0.8" fill="#7a3030" />
      <rect class="tama-chair-legs" x="19.6" y="18.2" width="0.9" height="0.8" fill="#4a2f22" />
      <g class="tama-book">
        <rect x="17.4" y="10.6" width="2.9" height="3.4" fill="#f2ece0" />
        <rect x="20.3" y="10.6" width="2.9" height="3.4" fill="#ebe3d3" />
        <g fill="#c9bfae">
          <rect x="17.9" y="11.4" width="1.9" height="0.3" />
          <rect x="17.9" y="12.3" width="1.6" height="0.3" />
          <rect x="20.8" y="11.4" width="1.9" height="0.3" />
          <rect x="20.8" y="12.3" width="1.5" height="0.3" />
        </g>
        <rect class="tama-page" x="20.3" y="10.6" width="2.9" height="3.4" fill="#f6f1e6" />
        <rect x="20.15" y="10.6" width="0.3" height="3.4" fill="#b9ae9b" />
      </g>
    </g>
  );
}

// Clawd's coding moods, from the usage trend (tamagotchi.codingMood). Grumpy:
// brows down, eyes narrowed, typing twice as fast, one red line on screen.
// Furious, "red-hot": a deep pulsing flush and a scowl, hammering hands, keys
// flying, the screen full of errors, smoke off the keyboard, heat shimmer,
// the desk rattling. The face sits in Clawd's group so it bobs and shakes
// with it; the hands and desk props use the coding pose's fixed desk; the
// screens are CodeScreen's.
function MoodFace({ x0, mood }) {
  return (
    <g class="tama-mood">
      {mood === "furious" && (
        <g class="tama-flush" fill="#ff3b2f" opacity="0.5">
          <rect x={x0 + 3} y={Y0} width="12" height="4" />
          <rect x={x0 + 1} y={Y0 + 4} width="16" height="2" />
          <rect x={x0 + 3} y={Y0 + 6} width="12" height="2" />
        </g>
      )}
      <g class="tama-brows" fill={EYE}>
        <rect x={x0 + 5} y={Y0 + 2.9} width="1" height="1.1" />
        <rect x={x0 + 12} y={Y0 + 2.9} width="1" height="1.1" />
        <rect x={x0 + 3.9} y={Y0 + 1.5} width="1.3" height="0.55" />
        <rect x={x0 + 5.1} y={Y0 + 2.05} width="1.3" height="0.55" />
        <rect x={x0 + 11.6} y={Y0 + 2.05} width="1.3" height="0.55" />
        <rect x={x0 + 12.8} y={Y0 + 1.5} width="1.3" height="0.55" />
      </g>
      {mood === "furious" && (
        <path class="tama-scowl" fill="none" stroke={EYE} stroke-width="0.35" shape-rendering="geometricPrecision"
              d={`M${x0 + 7.4} ${Y0 + 6} l0.8 -0.6 l0.8 0.6 l0.8 -0.6 l0.8 0.6`} />
      )}
      <g fill={SKIN}>
        <rect class="tama-hand" x="20.7" y="11.9" width="1.1" height="0.8" />
        <rect class="tama-hand tama-hand2" x="22.7" y="11.9" width="1.1" height="0.8" />
      </g>
      <g class="tama-clack" fill="#e6e9ef">
        <rect x="21.1" y="10.9" width="0.25" height="0.55" />
        <rect x="23.3" y="10.8" width="0.25" height="0.55" />
      </g>
    </g>
  );
}

// Furious only, and only what has to draw over Clawd and the desk: flying
// keys, smoke off the keyboard, heat shimmer, the spilling mug. (The screens
// are CodeScreen's, inside the desk.)
function MoodDesk({ mood, x0, coffee, hat, headDy }) {
  if (mood !== "furious") return null;
  return (
    <g>
      <g fill="#c9ced6">
        <rect class="tama-flying-key" x="21.4" y="12" width="0.7" height="0.7" />
        <rect class="tama-flying-key tama-flying-key2" x="23.1" y="12" width="0.7" height="0.7" />
        <rect class="tama-flying-key tama-flying-key3" x="22.3" y="12" width="0.6" height="0.6" />
      </g>
      <g fill="#8b93a1" shape-rendering="geometricPrecision">
        <circle class="tama-smoke" cx="21.6" cy="12.2" r="0.5" />
        <circle class="tama-smoke tama-smoke2" cx="23.4" cy="12.2" r="0.45" />
      </g>
      {!hat && (
        // Rises off the head (Clawd's top is Y0=9); a seated critter's head
        // top is higher, so the shimmer rides it up through Head instead of
        // starting inside the critter's head.
        <Head dy={headDy}>
          <g class="tama-shimmer" fill="none" stroke="#ff8a5c" stroke-width="0.3" opacity="0.8" shape-rendering="geometricPrecision">
            {[6, 9, 12].map((dx, i) => (
              <path key={dx} class={i === 1 ? "tama-shimmer2" : undefined}
                    d={`M${x0 + dx} ${i === 1 ? 7.2 : 7.6} q0.5 -0.7 0 -1.4 q-0.5 -0.7 0 -1.4`} />
            ))}
          </g>
        </Head>
      )}
      {coffee && (
        <g class="tama-spill" fill="#7a4e2d" shape-rendering="geometricPrecision">
          <circle cx="25.4" cy="11.1" r="0.25" />
          <circle class="tama-spill2" cx="26" cy="11" r="0.22" />
        </g>
      )}
    </g>
  );
}

// Where each duty kind shows (clawdDuty.js picks the kind). At the desk Clawd
// acts it out; awake anywhere else only the face kinds change Clawd (and a "!"
// for the bell); asleep, only the bell wakes it. The bubble carries every kind
// in every scene.
const FACE_KINDS = new Set(["bell", "done", "merged", "api", "ctx"]);

// The monitor's screen at the desk (clawdDuty.deskView decides which):
// drawn inside Behind's desk, under the morning mug, so it rattles with the
// monitor and never covers the mug. null keeps CodeScreen (the mood's lines).
function deskScreen({ activity, view, kind, duty, working, walled, mirroring }) {
  if (activity === "nap") return <WallScreen {...walled} />;
  if (!view.atDesk) return null;
  if (kind === "done") return <DoneScreen />;
  if (kind === "merged") return <MergeScreen />;
  if (kind === "ci") return <CiScreen />;
  if (kind === "api") return <ApiScreen />;
  if (kind === "ctx") return <CtxScreen pct={duty?.pct} cost={duty?.cost} />;
  return mirroring ? <PanesScreen n={working} /> : null;
}

function DeskProps({ kind }) {
  return (
    <g>
      {kind === "bell" && <ServiceBell />}
      {kind === "ctx" && <PaperStack />}
      {kind === "update" && <Parcel />}
    </g>
  );
}

// `activity` is the schedule's (tamagotchi.activityAt), the walled nap
// included; `walled` only gives the nap's monitor its reset time.
export function TamaScene({
  activity, season = "summer", occasion = null, coffee = false, mood = "calm", petted = false, petKey = 0,
  duty = null, working, walled = null, party = false, dish, trick = false, critter = CRITTERS[0],
}) {
  const kind = duty?.kind || null;
  const view = deskView(activity, kind);
  const { atDesk } = view;
  // Only the Halloween evening's rounds (tamagotchi.activityAt's `trick`) go
  // out in sheets: a Halloween-weekend afternoon run is an ordinary run.
  const ghost = trick;
  // A pastime's record, with what changes on a Halloween round.
  const base = PASTIME_SCENES[activity];
  const p = base && trick ? { ...base, ...base.trick } : base;
  const { x0, sitting = false, lift = 0 } = p?.pose || POSES[activity] || STANDING;
  const jack = p ? p.jack : JACK_SPOTS[activity];
  // Any creature but Clawd is a sprite (critters.js) standing in Clawd's spot,
  // showing its own moods and shutting its own eyes. Clawd's overlay
  // components stay Clawd's; a critter draws its own duty faces (`critterFace`
  // → Critter.jsx); a critter holds the pint, mug, spoon, can and leash in its
  // own appendage (`holding`), the bass by its limb reaching the fret
  // (`fret` below, Critter's `tip`), and the controller in its paws (no
  // reach). A critter types, cheers and slaps the bell with its own
  // appendages too (critters.js cheerGlyphs, TamaDuty TypingPaws / SlapPaw);
  // it also strides on the run, the bike and the dog walk, and sits without
  // the legs the seat hides (`seated`/`stride` below, critters.js
  // strideRoot/placement); only Clawd's own drawings of those (MoodFace's
  // typing hands, ArmsUp, SlapHand, LEG_CELLS) stay his.
  const sprite = critter.sprite;
  // The sheet is cut for Clawd; at CRITTER_HEIGHT ears, horns and antennas
  // poke through it.
  const place = sprite && placement(sprite, { x0, sitting, height: ghost ? CLAWD_HEIGHT : CRITTER_HEIGHT });
  // r4'd again: place.top is already rounded, but subtracting Y0 can still
  // surface a float a digit short of it (e.g. -2.5881999999999996 for -2.5882).
  const headDy = place ? r4(place.top - Y0) : 0;
  // Asleep — the night's sleep or the walled nap at the desk — only the bell
  // wakes Clawd: to an alarm clock at night, to the desk's service bell in the
  // nap.
  const sleeping = activity === "sleep" || activity === "nap";
  const ringing = kind === "bell" && sleeping;
  const asleep = sleeping && !ringing;
  const deskBell = kind === "bell" && (atDesk || activity === "nap");
  const pose = ghost || asleep ? null : atDesk || FACE_KINDS.has(kind) ? kind : null;
  const celebrating = !ghost && !asleep && (pose === "done" || pose === "merged");
  const fullParty = pose === "merged";
  const hat = party && !ghost && !asleep ? "party"
    : occasion === "yearend" ? "santa" : occasion === "newyear" ? "party" : null;
  // Only the coding scene has moods, and anything on duty outranks them.
  const moody = view.moodShown && MOODS[mood]?.rank > 0;
  // The monitor mirrors the working agents unless the top kind needs it or a
  // mood (only ever shown with nothing on duty) has it; before the first
  // /api/state the pane count is unknown, so the code stays.
  const mirroring = view.showsPanes && working != null && !moody;
  const screen = deskScreen({ activity, view, kind, duty, working, walled, mirroring });
  const face = ghost ? <GhostSheet x0={x0} />
    : sprite ? null
    : moody ? <MoodFace x0={x0} mood={mood} />
    : asleep ? <Eyes closed x0={x0} />
    : celebrating ? <HappyEyes x0={x0} />
    : pose === "api" ? <XEyes x0={x0} />
    : pose === "update" ? <LookDown x0={x0} />
    : !pose && p?.smile ? <HappyEyes x0={x0} />
    : <Eyes x0={x0} />;
  // A critter's duty face (Critter.jsx draws it over its eyes): Clawd's rules
  // above, with the moods left to the critter's own layers.
  const critterFace = !sprite || ghost || asleep || moody ? null
    : celebrating ? "happy"
    : pose === "api" ? "x"
    : pose === "update" ? "down"
    : pose === "ctx" ? "worried"
    : !pose && p?.smile ? "happy"
    : null;
  // What a critter wears for the pastime, rebuilt on its own eyes and neck
  // (critters.js wornGlyphs); Clawd's Goggles and Scarf fit only Clawd.
  const worn = sprite && p?.Held === Goggles ? "goggles" : sprite && p?.Held === Scarf ? "scarf" : null;
  const holding = sprite ? HOLDING[activity] ?? null : null;
  // Where a critter's tip is, for the props drawn outside its wrapper (the
  // spoon into the pot, the leash to the dog): through the same `place` the
  // wrapper uses, so Spoon and Dog get a point and never a placement.
  const holdFrom = holding === "spoon" || holding === "leash" ? holdTipInScene(sprite, holding, place) : null;
  // The bass sits in the critter's wrapper, mapped back to scene units, as
  // Critter's child: over its body and under its limb, which frets the neck in
  // front of it at Clawd's fret spot.
  const fret = holding === "bass" ? sceneToSprite(place, [x0 + BASS_FRET.dx, BASS_FRET.y]) : null;
  // At the desk a critter's limb reaches the keyboard when it's moody and the
  // service bell when the bell's on duty there (TamaDuty KEYS_REACH /
  // BELL_REACH), through the placement it's drawn with, like the fret.
  const deskTip = sprite && (moody ? KEYS_REACH : deskBell ? BELL_REACH : null);
  const tip = fret ?? (deskTip ? sceneToSprite(place, deskTip) : null);
  return (
    <svg viewBox="0 0 34 24" shape-rendering="crispEdges" aria-hidden="true"
         data-activity={activity} data-occasion={occasion || undefined} data-mood={moody ? mood : undefined}
         data-duty={kind || undefined} data-pose={pose || undefined} data-urgent={duty?.urgent ? "" : undefined}
         data-working={mirroring ? Math.min(working, 4) : undefined} data-critter={sprite ? critter.id : undefined}
         data-tempo={p?.tempo} data-stride={p?.stride ? "" : undefined} style={p && `--tama-tempo: ${p.tempo}s`}>
      <rect x="0" y="19" width="34" height="0.4" fill="var(--line)" />
      {(occasion === "newyear" || fullParty) && <Fireworks clear={!!duty} />}
      {p ? <PastimeBehind p={p} season={season} trick={trick} critter={critter} from={holdFrom} /> : (
        <Behind activity={activity === "nap" ? "code" : activity} occasion={occasion} coffee={coffee} ringing={ringing}
                keyboard={!(deskBell || (atDesk && kind === "ctx"))} mood={moody ? mood : "calm"} screen={screen}
                dish={dish} mugHeld={holding === "mug"} />
      )}
      {occasion === "halloween" && jack && <JackOLantern {...jack} />}
      {/* The lift sits on a wrapper: the creature's CSS bob sets `transform`. */}
      <g transform={lift ? `translate(0 ${-lift})` : undefined}>
      <g key={petKey} class={`tama-creature${petted ? " tama-hop" : ""}`}>
        {sprite ? (
          // Placement is an attribute on a wrapper no animation targets: a CSS
          // transform (the creature's bob, shake, hop) would override it.
          // transform-box: view-box overrides styles.css's `.tama g { transform-box:
          // fill-box }`, which would otherwise pivot scale() around this group's
          // own (mood- and animation-dependent) bounding box instead of 0,0.
          <g class="tama-critter" style="transform-box: view-box"
             transform={`translate(${place.tx} ${place.ty}) scale(${place.scale})`}>
            <Critter sprite={sprite} mood={moody ? mood : "calm"} asleep={asleep} face={critterFace}
                     startle={pose === "bell" && !deskBell} worn={worn} holding={holding} tip={tip} paws={!!p?.paws}
                     cheer={atDesk && celebrating} seated={sitting} stride={!!p?.stride}>
              {fret && (
                // Undoes the wrapper's placement exactly, so the bass draws at
                // Clawd's fixed scene coordinates while still bobbing as the
                // wrapper's child. view-box, not critters.css's `.critter g`
                // fill-box default: fill-box would pivot scale() on this
                // group's own box instead of 0,0, same reason as the wrapper's.
                <g style="transform-box: view-box" transform={`scale(${1 / place.scale}) translate(${-place.tx} ${-place.ty})`}>
                  <Bass x0={x0} limb={sprite.limb} />
                </g>
              )}
            </Critter>
            <CritterHats sprite={sprite} p={p} hat={hat} season={season} ghost={ghost} />
            {holding && <CritterHeld sprite={sprite} kind={holding} scale={place.scale} />}
          </g>
        ) : <Sprite cells={p?.paws ? PAWS_BODY_CELLS : BODY_CELLS} cls="tama-body" x0={x0} />}
        {face}
        {p?.Held && !(sprite && (WORN_ON_CLAWD.has(p.Held) || holding)) && <p.Held x0={x0} />}
        {holding === "spoon" && <Spoon x0={x0} from={holdFrom} />}
        {holding === "controller" && (
          // Drawn at Clawd's fixed spot, not through the critter's own
          // wrapper (CritterHeld's `scale`, not this one): a seated critter
          // with hidden legs sits lower than Clawd, so it lowers with the
          // body (`place.drop`) but never past the seat (0.8 = SEAT 17 minus
          // the controller's own bottom, 16.2).
          <g transform={`translate(0 ${Math.min(place.drop, 0.8)})`}>
            <Controller x0={x0} limb={sprite.limb} />
          </g>
        )}
        {!sprite && <Head dy={headDy}>{p?.Headwear && !hat && <p.Headwear x0={x0} />}</Head>}
        <Head dy={headDy}>{pose === "api" && <Stars x0={x0} />}</Head>
        {!sprite && pose === "ctx" && <Worried x0={x0} />}
        {!sprite && atDesk && celebrating && <ArmsUp x0={x0} />}
        {atDesk && pose === "ci" && <Extinguisher />}
        {!sprite && deskBell && <SlapHand />}
        {sprite && moody && <TypingPaws limb={sprite.limb} />}
        {sprite && deskBell && <SlapPaw limb={sprite.limb} />}
        {!sprite && <Head dy={headDy}>{pose === "bell" && !deskBell && <Startle x0={x0} />}</Head>}
        {!sprite && !sitting && <Sprite cells={LEG_CELLS[0]} cls="tama-legs-a" x0={x0} y0={LEGS_Y0} />}
        {!sprite && p?.stride && <Sprite cells={LEG_CELLS[1]} cls="tama-legs-b" x0={x0} y0={LEGS_Y0} />}
        {ghost && p?.pail && <Pail x0={x0} />}
        {!sprite && (
          <Head dy={headDy}>
            {p?.snowcap && season === "winter" && !hat && !ghost && (
              <rect class="tama-snowcap" x={x0 + 3} y="8.4" width="12" height="0.7" rx="0.3" fill="#f4f8fb" />
            )}
            {hat === "santa" && <SantaHat x0={x0} />}
            {hat === "party" && <PartyHat x0={x0} />}
          </Head>
        )}
      </g>
      </g>
      {p?.Front && <p.Front />}
      <InFront activity={activity} />
      {(atDesk || deskBell) && <DeskProps kind={kind} />}
      {activity === "nap" && !ringing && <DeskZs />}
      {ringing && activity === "sleep" && <AlarmClock />}
      {celebrating && <Sparkles />}
      {moody && <MoodDesk mood={mood} x0={x0} coffee={coffee} hat={hat} headDy={headDy} />}
      {(occasion === "newyear" || fullParty) && <Confetti />}
      {duty && <Bubble kind={kind} label={duty.label} urgent={duty.urgent} more={duty.more} x0={x0} />}
      {petted && (
        // The pose offset sits on a wrapper: the heart's CSS float animation
        // sets `transform`, which would override a transform attribute here.
        <g key={`heart-${petKey}`} transform={`translate(${x0 - 8} 0)`}>
          <path class="tama-heart" fill="#e8657a" shape-rendering="geometricPrecision"
                d="M17 7.2 l-2.3 -2.3 a1.25 1.25 0 0 1 2.3 -1.7 a1.25 1.25 0 0 1 2.3 1.7z" />
        </g>
      )}
    </svg>
  );
}

// The picked critter, as its own signal: prefsSignal is one blob of every ui
// pref, and reading it in render would rebuild the scene on any pin, reorder
// or splitter drag. critterById returns the same object for the same id, so
// this only notifies when the pick changes.
const pickedCritter = computed(() => critterById(getTamaCritter()));

// What the schedule knows about today (tamagotchi.activityAt's `day`): the
// day log (prefs.getClawdDay) and the stretches every account was walled, as
// the server dated them (`wall_spans`, not `walls`: the first usage refresh
// past the reset drops those, and the knock-off half under way went back to
// the desk with them). A stretch with no reset reported holds for good. Read
// from signals, not render state, so the minute backstop's closure sees
// today's, not the first render's.
const wallsOf = (spans) => spans.map((s) => [s.since * 1000, s.until == null ? Infinity : s.until * 1000]);
const dayAt = (d) => ({ ...dayFlags(getClawdDay(), d), walls: wallsOf(wallSpans.value) });

export function Tamagotchi() {
  const [now, setNow] = useState(() => new Date());
  const [pets, setPets] = useState(0);
  const [petted, setPetted] = useState(false);

  // The day log and the walls' stretches, as a string key: /api/state
  // replaces `wall_spans` every poll and any prefs write replaces the prefs
  // signal, so reading them in render rebuilt the scene each time, for
  // nothing new.
  const dayLog = useComputed(() => {
    const log = getClawdDay();
    return JSON.stringify([log.merged ?? null, log.furious ?? null, wallSpans.value]);
  });
  const dayDeps = dayLog.value;
  const [merged, furious, spans] = JSON.parse(dayDeps);
  const day = { ...dayFlags({ merged, furious }, now), walls: wallsOf(spans) };

  // Wake when the current window ends, or the walls start or stop holding. A
  // wall or a verdict landing moves the end (a walled weekday knocks off at
  // 4pm), and `now` is when the current window began, so it re-anchors on
  // the clock first: from `now`, the scene and the wake were both the old
  // window's until the minute backstop caught up.
  const anchored = useRef(dayDeps);
  useEffect(() => {
    if (anchored.current !== dayDeps) {
      anchored.current = dayDeps;
      setNow(new Date());
      return;
    }
    const t = setTimeout(() => setNow(new Date()), msUntilNextChange(now, day) + 50);
    return () => clearTimeout(t);
  }, [now, dayDeps]);
  // Minute backstop for a laptop that slept through the boundary. Keeps the
  // old date while nothing drawn would change, so the ~250-node SVG isn't
  // rebuilt every minute for the dozen changes a day.
  useEffect(() => {
    const i = setInterval(() => {
      const d = new Date();
      setNow((prev) => (sceneKey(d, dayAt(d)) === sceneKey(prev, dayAt(prev)) ? prev : d));
    }, 60_000);
    return () => clearInterval(i);
  }, []);

  useEffect(() => {
    if (!petted) return;
    const t = setTimeout(() => setPetted(false), 900);
    return () => clearTimeout(t);
  }, [petted, pets]);

  // A minute tick for the stale check below: a failed poll leaves `usage`
  // untouched, so without it a hot reading from a server that's gone away
  // would keep Clawd furious forever rather than going stale and calm.
  const minute = useSignal(0);
  useEffect(() => {
    const i = setInterval(() => { minute.value += 1; }, 60_000);
    return () => clearInterval(i);
  }, []);

  useEffect(() => { startClawdWatch(); }, []);

  const critter = pickedCritter.value;

  // The coding mood, as a string key: /api/state replaces `usage` every poll,
  // and a poll that leaves the mood unchanged mustn't rebuild the scene.
  const moodKey = useComputed(() => {
    minute.value;
    const plan = usage.value?.plan;
    const r = codingMood(plan, Date.now() / 1000);
    const name = r.account && multiAccount() ? accountLabel(r.account) : null;
    return JSON.stringify([r.mood, moodReason(r, name)]);
  });
  const [mood, reason] = JSON.parse(moodKey.value);
  // A furious day ends in a run (tamagotchi.activityAt), booked from the time
  // it's noted. Keyed on the date as well, so a mood still furious past
  // midnight counts for the new day too.
  useEffect(() => {
    if (mood === "furious") noteClawdDay("furious", noteKey(new Date()));
  }, [mood, dayKey(now)]);

  // What Clawd is on duty for, also as a string key: every poll replaces
  // `windows`, and one that changes nothing on duty mustn't rebuild the scene.
  // The hover's wait times tick by the minute, so a long wait re-renders once
  // a minute, not once a poll.
  const dutyKey = useComputed(() => {
    minute.value; // wait times and the party's ten minutes move on without a poll
    const nowSec = Date.now() / 1000;
    const items = dutyItems({
      windows: windows.value || [], alerts: alerts.value || [],
      dismissedAlertIds: dismissedAlertIds.value, dismissedNeedsPids: dismissedNeedsPids.value,
      dismissedReadyPids: dismissedReadyPids.value, selectedPid: selectedPid(railSelection.value),
      parties: mergeParties.value, update: updateInfo.value, nowSec,
    });
    const w = walls.value;
    return JSON.stringify({
      items, more: moreCount(items), wall: w && wallLabel(w.until, nowSec),
      // `alerts` stays null until the first /api/state: the pane count is unknown, not zero.
      working: alerts.value === null ? null : buildRunning(windows.value).length,
    });
  });
  const d = JSON.parse(dutyKey.value);
  const top = d.items[0] || null;

  const a = activityAt(now, day);
  const next = upNext(now, day);
  const occasion = occasionShown(now);
  const headline = headlineAt(now);
  // The same view TamaScene draws from, so the hover can't describe another scene.
  const view = deskView(a.id, top?.kind);
  const doing = activityLabel(a, view.moodShown ? mood : "calm");
  const parts = [
    // Not past the reset (the server republishes the walls only with the
    // next usage refresh): the scene is the plain day's by then.
    ...(d.wall && walledAt(now, day) ? [wallPhrase(d.wall)] : []),
    ...d.items.map((i) => i.phrase),
    ...(view.showsPanes && d.working ? [`${plural(d.working, "agent")} working`] : []),
    ...(view.moodShown && reason ? [reason] : []),
    ...(headline ? [headline] : []),
    ...(next ? [`up next at ${clockLabel(next.at)}: ${activityLabel(next.activity)}`] : []),
  ];
  // Clawd keeps its banner name; any other critter goes by its own.
  const label = `${critter.sprite ? critter.name : "Claude"} is ${doing} (${hourLabel(a.from)}–${hourLabel(a.to)})${parts.map((x) => ` · ${x}`).join("")}`
    + (top?.target ? ` · click to ${top.target.says}` : "");

  function pet() { setPets((n) => n + 1); setPetted(true); }
  // Clicks (clawdClick.js) read the bubble's current item through a ref, so
  // the handlers — and their pending double-click timer — live for the widget.
  const topRef = useRef(top);
  topRef.current = top;
  const [click] = useState(() => clickHandlers({ target: () => topRef.current?.target, pet }));
  useEffect(() => () => click.dispose(), []);
  return (
    <button
      type="button"
      class="tama"
      title={label}
      aria-label={label}
      // The second click of a double-click is the pet, not a second action.
      onClick={click.onClick}
      onDblClick={click.onDblClick}
    >
      <TamaScene activity={a.id} dish={a.dish} trick={!!a.trick} season={seasonAt(now)} occasion={occasion?.id} coffee={!!a.coffee}
                 mood={mood} petted={petted} petKey={pets}
                 duty={top && { kind: top.kind, label: top.label, urgent: !!top.urgent, more: d.more, pct: top.pct, cost: top.cost }}
                 working={d.working} walled={d.wall} party={d.items.some((i) => i.kind === "merged")}
                 critter={critter} />
    </button>
  );
}
