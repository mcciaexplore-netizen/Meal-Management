import assert from "node:assert/strict";
import test from "node:test";
import { decodePixels, decodeSource, nativeQrDetector } from "../src/qr_decoder.js";

const matrix = [
  "00000000000000000000000000000",
  "00000000000000000000000000000",
  "00000000000000000000000000000",
  "00000000000000000000000000000",
  "00001111111001010011111110000",
  "00001000001010011010000010000",
  "00001011101000101010111010000",
  "00001011101000111010111010000",
  "00001011101011011010111010000",
  "00001000001001010010000010000",
  "00001111111010101011111110000",
  "00000000000000000000000000000",
  "00001010101001001000100100000",
  "00001111100011110100101110000",
  "00000010101001010101100110000",
  "00001101110011011000000000000",
  "00001100011111010101100110000",
  "00000000000010010001111010000",
  "00001111111001101011111110000",
  "00001000001001011101000100000",
  "00001011101011101001110100000",
  "00001011101001101010100100000",
  "00001011101011111111101010000",
  "00001000001001100000010100000",
  "00001111111010001101000110000",
  "00000000000000000000000000000",
  "00000000000000000000000000000",
  "00000000000000000000000000000",
  "00000000000000000000000000000"
];

function pixels(invert = false) {
  const width = matrix.length * 5;
  const data = new Uint8ClampedArray(width * width * 4);
  for (let y = 0; y < width; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const dark = matrix[Math.floor(y / 5)][Math.floor(x / 5)] === "1";
      const color = dark !== invert ? 0 : 255;
      const index = (y * width + x) * 4;
      data[index] = color;
      data[index + 1] = color;
      data[index + 2] = color;
      data[index + 3] = 255;
    }
  }
  return { data, width, height: width };
}

test("jsQR fallback decodes a real QR pixel matrix", () => {
  assert.equal(decodePixels(pixels()), "unit-test-qr");
});

test("jsQR fallback handles inverted QR colors", () => {
  assert.equal(decodePixels(pixels(true)), "unit-test-qr");
});

test("images with no QR return no credential", () => {
  const blank = { data: new Uint8ClampedArray(100 * 100 * 4).fill(255), width: 100, height: 100 };
  assert.equal(decodePixels(blank), null);
});

test("native decoding failures fall back to jsQR for image and camera sources", async () => {
  const image = pixels();
  const source = { videoWidth: image.width, videoHeight: image.height };
  const canvas = { getContext: () => ({ drawImage() {}, getImageData: () => image }) };
  const detector = { detect: async () => { throw new Error("Native decoder unavailable"); } };
  assert.equal(await decodeSource(source, canvas, detector), "unit-test-qr");
});

test("native QR decoding can operate without a canvas context", async () => {
  const detector = { detect: async () => [{ rawValue: "unit-test-qr" }] };
  assert.equal(await decodeSource({}, {}, detector), "unit-test-qr");
});

test("native detector is optional and must support QR format", async () => {
  assert.equal(await nativeQrDetector(null), null);
  class Unsupported {
    static async getSupportedFormats() { return ["ean_13"]; }
  }
  assert.equal(await nativeQrDetector(Unsupported), null);
  class Supported {
    static async getSupportedFormats() { return ["qr_code"]; }
    constructor(config) { this.formats = config.formats; }
  }
  assert.deepEqual((await nativeQrDetector(Supported)).formats, ["qr_code"]);
});
