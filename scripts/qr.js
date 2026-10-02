// Render an ASCII QR code for a URL, using the `toqr` encoder that already
// ships inside mobile_app/node_modules (MIT, no new dependency).
//
// Usage: node scripts/qr.js "exp://10.175.26.85:8081"
//
// Deliberately ASCII-only ('#' and space). The usual half-block rendering (U+2580
// and friends) is compact and pretty, but a Windows console runs on code page
// 437/1252 and turns those glyphs into mojibake - and a QR code that renders as
// mojibake does not scan. Two characters per module horizontally gives the 1:1
// aspect ratio a scanner needs without any non-ASCII output.

const url = process.argv[2];
if (!url) {
  console.error("usage: node scripts/qr.js <text>");
  process.exit(2);
}

let toQR;
try {
  ({ toQR } = require("../mobile_app/node_modules/toqr"));
} catch (err) {
  // Never let a missing optional encoder break start.bat.
  process.exit(3);
}

let matrix;
try {
  matrix = toQR(url);
} catch (err) {
  process.exit(3);
}

// toQR returns a flat {index: 0|1} map with no size field; the QR side is the
// integer square root of the entry count (e.g. 625 entries -> 25x25, version 2).
const cells = Object.keys(matrix).length;
const size = Math.round(Math.sqrt(cells));
if (size * size !== cells) {
  process.exit(3);
}

const at = (row, col) => matrix[row * size + col] ? 1 : 0;

// A quiet zone is part of the QR specification: scanners need a light margin
// around the modules or a code sitting next to other console text can fail.
const QUIET = 3;
const width = size + QUIET * 2;

const lines = [];
for (let row = -QUIET; row < size + QUIET; row += 1) {
  let line = "";
  for (let col = -QUIET; col < size + QUIET; col += 1) {
    const inRange = row >= 0 && row < size && col >= 0 && col < size;
    const dark = inRange && at(row, col);
    line += dark ? "##" : "  ";
  }
  // Leading/trailing light rows are trimmed to a single space so the block is
  // compact but the margin itself is preserved in the dark rows.
  lines.push(line);
}

process.stdout.write(lines.join("\n") + "\n");