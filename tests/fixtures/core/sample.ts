import type { Config } from "./config";

/** A named thing. */
export interface Named {
  name: string;
}

type Id = string;

/** Builds a widget. */
export function build(config: Config): Named {
  return { name: "w" };
}

export class Service {
  /** Runs it. */
  run(): void {}
}
