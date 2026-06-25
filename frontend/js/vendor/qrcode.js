/**
 * Minimal QR code generator — byte mode, error-correction level L, versions
 * 1-10 (enough for an otpauth:// URI). Self-contained, zero dependencies, no
 * network. Renders into a <canvas>.
 *
 * Vendored for AgeniusDesk 2FA enrollment. MIT-style standalone implementation
 * of the public QR Code spec (ISO/IEC 18004). Not a general-purpose library:
 * it deliberately supports only what the 2FA flow needs.
 *
 * Usage:
 *   import { renderQR } from '../vendor/qrcode.js';
 *   renderQR(canvasEl, 'otpauth://...', { scale: 5 });
 */

// ── Galois field GF(256) for Reed-Solomon ────────────────────────────────────
const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
(function initGF() {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();

function gfMul(a, b) {
  if (a === 0 || b === 0) return 0;
  return EXP[LOG[a] + LOG[b]];
}

function rsGenerator(degree) {
  let poly = [1];
  for (let i = 0; i < degree; i++) {
    const next = new Array(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= poly[j];
      next[j + 1] ^= gfMul(poly[j], EXP[i]);
    }
    poly = next;
  }
  return poly;
}

function rsEncode(data, ecLen) {
  const gen = rsGenerator(ecLen);
  const res = new Array(ecLen).fill(0);
  for (const d of data) {
    const factor = d ^ res[0];
    res.shift();
    res.push(0);
    for (let i = 0; i < ecLen; i++) res[i] ^= gfMul(gen[i], factor);
  }
  return res;
}

// ── Per-version tables (ECC level L only) ────────────────────────────────────
// [version]: { ec: ecCodewordsPerBlock, groups: [[numBlocks, dataCodewords], ...] }
const VER = {
  1:  { ec: 7,  groups: [[1, 19]] },
  2:  { ec: 10, groups: [[1, 34]] },
  3:  { ec: 15, groups: [[1, 55]] },
  4:  { ec: 20, groups: [[1, 80]] },
  5:  { ec: 26, groups: [[1, 108]] },
  6:  { ec: 18, groups: [[2, 68]] },
  7:  { ec: 20, groups: [[2, 78]] },
  8:  { ec: 24, groups: [[2, 97]] },
  9:  { ec: 30, groups: [[2, 116]] },
  10: { ec: 18, groups: [[2, 68], [2, 69]] },
};
const ALIGN = {
  1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
  6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
};

function dataCapacity(v) {
  return VER[v].groups.reduce((sum, [n, d]) => sum + n * d, 0);
}

function chooseVersion(byteLen) {
  // header = mode(4) + charCount(8 for v1-9, 16 for v10) + terminator etc.
  for (let v = 1; v <= 10; v++) {
    const ccBits = v >= 10 ? 16 : 8;
    const need = 4 + ccBits + byteLen * 8;
    if (need <= dataCapacity(v) * 8) return v;
  }
  throw new Error('QR payload too long');
}

// ── Bit buffer ───────────────────────────────────────────────────────────────
class Bits {
  constructor() { this.arr = []; }
  put(val, len) { for (let i = len - 1; i >= 0; i--) this.arr.push((val >> i) & 1); }
  get length() { return this.arr.length; }
}

function buildData(text, version) {
  const bytes = new TextEncoder().encode(text);
  const bits = new Bits();
  bits.put(0b0100, 4); // byte mode
  bits.put(bytes.length, version >= 10 ? 16 : 8);
  for (const b of bytes) bits.put(b, 8);

  const totalData = dataCapacity(version);
  const capacityBits = totalData * 8;
  // Terminator (up to 4 bits).
  for (let i = 0; i < 4 && bits.length < capacityBits; i++) bits.arr.push(0);
  // Pad to byte boundary.
  while (bits.length % 8 !== 0) bits.arr.push(0);
  // Pad bytes.
  const padBytes = [0xec, 0x11];
  let pi = 0;
  const codewords = [];
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j++) byte = (byte << 1) | bits.arr[i + j];
    codewords.push(byte);
  }
  while (codewords.length < totalData) { codewords.push(padBytes[pi % 2]); pi++; }
  return codewords;
}

function interleave(codewords, version) {
  const { ec, groups } = VER[version];
  const blocks = [];
  let offset = 0;
  for (const [numBlocks, dataLen] of groups) {
    for (let b = 0; b < numBlocks; b++) {
      const data = codewords.slice(offset, offset + dataLen);
      offset += dataLen;
      blocks.push({ data, ecc: rsEncode(data, ec) });
    }
  }
  const result = [];
  const maxData = Math.max(...blocks.map(b => b.data.length));
  for (let i = 0; i < maxData; i++) {
    for (const blk of blocks) if (i < blk.data.length) result.push(blk.data[i]);
  }
  for (let i = 0; i < ec; i++) {
    for (const blk of blocks) result.push(blk.ecc[i]);
  }
  return result;
}

// ── Matrix construction ──────────────────────────────────────────────────────
function buildMatrix(finalCodewords, version) {
  const size = 17 + version * 4;
  const m = Array.from({ length: size }, () => new Array(size).fill(null));
  const reserved = Array.from({ length: size }, () => new Array(size).fill(false));

  function place(r, c, val) { m[r][c] = val; reserved[r][c] = true; }

  // Finder + separators.
  function finder(r, c) {
    for (let dr = -1; dr <= 7; dr++) {
      for (let dc = -1; dc <= 7; dc++) {
        const rr = r + dr, cc = c + dc;
        if (rr < 0 || rr >= size || cc < 0 || cc >= size) continue;
        const inRing = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6 &&
          (dr === 0 || dr === 6 || dc === 0 || dc === 6);
        const inCore = dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4;
        place(rr, cc, inRing || inCore ? 1 : 0);
      }
    }
  }
  finder(0, 0); finder(0, size - 7); finder(size - 7, 0);

  // Timing patterns.
  for (let i = 8; i < size - 8; i++) {
    place(6, i, i % 2 === 0 ? 1 : 0);
    place(i, 6, i % 2 === 0 ? 1 : 0);
  }

  // Alignment patterns.
  const centers = ALIGN[version];
  for (const r of centers) {
    for (const c of centers) {
      if (reserved[r][c]) continue; // skip ones overlapping finders
      for (let dr = -2; dr <= 2; dr++) {
        for (let dc = -2; dc <= 2; dc++) {
          const ring = Math.max(Math.abs(dr), Math.abs(dc));
          place(r + dr, c + dc, ring === 1 ? 0 : 1);
        }
      }
    }
  }

  // Dark module.
  place(size - 8, 8, 1);

  // Reserve format-info areas (filled later).
  for (let i = 0; i < 9; i++) {
    if (!reserved[8][i]) reserved[8][i] = true;
    if (!reserved[i][8]) reserved[i][8] = true;
  }
  for (let i = 0; i < 8; i++) {
    reserved[8][size - 1 - i] = true;
    reserved[size - 1 - i][8] = true;
  }

  // Lay data bits in zig-zag.
  const bitsArr = [];
  for (const cw of finalCodewords) for (let i = 7; i >= 0; i--) bitsArr.push((cw >> i) & 1);

  let bitIdx = 0;
  let upward = true;
  for (let col = size - 1; col > 0; col -= 2) {
    if (col === 6) col = 5; // skip vertical timing column
    for (let i = 0; i < size; i++) {
      const row = upward ? size - 1 - i : i;
      for (let c = 0; c < 2; c++) {
        const cc = col - c;
        if (reserved[row][cc]) continue;
        m[row][cc] = bitIdx < bitsArr.length ? bitsArr[bitIdx] : 0;
        bitIdx++;
      }
    }
    upward = !upward;
  }

  return { m, reserved, size };
}

// ── Masking ──────────────────────────────────────────────────────────────────
const MASKS = [
  (r, c) => (r + c) % 2 === 0,
  (r, c) => r % 2 === 0,
  (r, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
  (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

function applyMask(base, reserved, size, maskIdx) {
  const out = base.map(row => row.slice());
  const fn = MASKS[maskIdx];
  for (let r = 0; r < size; r++)
    for (let c = 0; c < size; c++)
      if (!reserved[r][c] && fn(r, c)) out[r][c] ^= 1;
  return out;
}

function penalty(m, size) {
  let score = 0;
  // Rule 1: runs of 5+ same color.
  for (let r = 0; r < size; r++) {
    for (const line of [m[r], m.map(row => row[r])]) {
      let run = 1;
      for (let i = 1; i < size; i++) {
        if (line[i] === line[i - 1]) { run++; if (run === 5) score += 3; else if (run > 5) score++; }
        else run = 1;
      }
    }
  }
  // Rule 2: 2x2 blocks.
  for (let r = 0; r < size - 1; r++)
    for (let c = 0; c < size - 1; c++)
      if (m[r][c] === m[r][c + 1] && m[r][c] === m[r + 1][c] && m[r][c] === m[r + 1][c + 1]) score += 3;
  return score;
}

// ── Format info (level L) ────────────────────────────────────────────────────
function formatBits(maskIdx) {
  const ecLevel = 0b01; // L
  let data = (ecLevel << 3) | maskIdx;
  let rem = data << 10;
  const g = 0b10100110111;
  for (let i = 14; i >= 10; i--) if ((rem >> i) & 1) rem ^= g << (i - 10);
  let bits = ((data << 10) | rem) ^ 0b101010000010010;
  return bits & 0x7fff;
}

function placeFormat(m, size, maskIdx) {
  const bits = formatBits(maskIdx);
  const get = i => (bits >> i) & 1;
  // Around top-left.
  for (let i = 0; i <= 5; i++) m[8][i] = get(i);
  m[8][7] = get(6); m[8][8] = get(7); m[7][8] = get(8);
  for (let i = 9; i <= 14; i++) m[14 - i][8] = get(i);
  // Around the other two finders.
  for (let i = 0; i <= 7; i++) m[size - 1 - i][8] = get(i);
  for (let i = 8; i <= 14; i++) m[8][size - 15 + i] = get(i);
}

export function generateMatrix(text) {
  const version = chooseVersion(new TextEncoder().encode(text).length);
  const codewords = buildData(text, version);
  const finalCw = interleave(codewords, version);
  const { m: base, reserved, size } = buildMatrix(finalCw, version);

  let best = null, bestScore = Infinity;
  for (let mask = 0; mask < 8; mask++) {
    const masked = applyMask(base, reserved, size, mask);
    placeFormat(masked, size, mask);
    const score = penalty(masked, size);
    if (score < bestScore) { bestScore = score; best = masked; }
  }
  return { matrix: best, size };
}

export function renderQR(canvas, text, opts = {}) {
  const scale = opts.scale || 5;
  const margin = opts.margin == null ? 4 : opts.margin;
  const { matrix, size } = generateMatrix(text);
  const dim = (size + margin * 2) * scale;
  canvas.width = dim;
  canvas.height = dim;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = opts.light || '#ffffff';
  ctx.fillRect(0, 0, dim, dim);
  ctx.fillStyle = opts.dark || '#000000';
  for (let r = 0; r < size; r++) {
    for (let c = 0; c < size; c++) {
      if (matrix[r][c]) {
        ctx.fillRect((c + margin) * scale, (r + margin) * scale, scale, scale);
      }
    }
  }
  return canvas;
}
