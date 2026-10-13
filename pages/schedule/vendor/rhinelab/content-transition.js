/**
 * Adapted from RhineLabUI src/ui-transitions.ts (ContentTransition).
 * Copyright (c) 2026 LBEILC
 * SPDX-License-Identifier: MIT
 * Upstream: https://github.com/LBEILC/RhineLabUI
 * Revision: 12cc5e4013acb9408f753ff71de6b8492299b7c8
 * Local change: removed TypeScript type annotations for native browser use.
 * The complete upstream license is provided in ./LICENSE.
 */

const enterEase = "cubic-bezier(0.22, 1, 0.36, 1)";

export class ContentTransition {
  animation;

  reveal(element, reduced) {
    const opacity =
      this.animation?.playState === "running"
        ? getComputedStyle(element).opacity
        : "0.35";
    this.cancel();
    if (!reduced)
      this.animation = element.animate([{ opacity }, { opacity: 1 }], {
        duration: 150,
        easing: enterEase,
      });
  }

  cancel() {
    this.animation?.cancel();
    this.animation = undefined;
  }

  finish() {
    this.animation?.finish();
    this.animation = undefined;
  }
}
