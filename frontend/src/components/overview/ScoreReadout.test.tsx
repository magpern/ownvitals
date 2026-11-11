import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fakeFrames,
  fakeIntersection,
  fakeMatchMedia,
} from "../motion/motionFakes";
import { ScoreReadout } from "./ScoreReadout";

// Synthetic score values only.

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ScoreReadout", () => {
  it("has the final score in the DOM from the first paint, a glow and no ring or arc", () => {
    fakeMatchMedia(false);
    fakeFrames();
    fakeIntersection();
    const { container } = render(<ScoreReadout value={78} />);
    expect(screen.getByText("78", { selector: ".ov-sr" })).toBeInTheDocument();
    expect(container.querySelector(".ov-score__glow")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(container.querySelector("svg, circle, path")).toBeNull();
  });

  it("under reduced motion shows the final number at once", () => {
    fakeMatchMedia(true);
    const frames = fakeFrames();
    fakeIntersection();
    const { container } = render(<ScoreReadout value={64} />);
    expect(container.querySelector(".ov-countup__live")).toHaveTextContent(
      "64",
    );
    expect(frames.pending()).toBe(0);
  });
});
