import fs from "node:fs";
const lodash = require("lodash");

/**
 * Adds two numbers.
 */
export function add(a, b) {
  return a + b;
}

export const scale = (a) => a * 2;

export class Box {
  // Returns the volume.
  volume() {
    return 1;
  }
}
