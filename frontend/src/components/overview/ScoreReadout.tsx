import { CountUp } from "../motion";

// The health score as a plain, very large number with a soft glow behind it (docs/design/motion.md,
// items 1 and 2; no ring, no arc). The count-up and the glow are the same for every value (rule 4).
// The glow is a static halo that fades in once; it never pulses.

interface Props {
  /** The score, 0-100, as the API sent it. */
  value: number;
  /** The score as it is read ("78"); also the accessible text. */
  format?: (n: number) => string;
}

export function ScoreReadout({
  value,
  format = (n) => String(Math.round(n)),
}: Props) {
  return (
    <div className="ov-score">
      <span className="ov-score__glow" aria-hidden="true" />
      <span className="ov-score__num">
        <CountUp value={value} format={format} />
      </span>
    </div>
  );
}
