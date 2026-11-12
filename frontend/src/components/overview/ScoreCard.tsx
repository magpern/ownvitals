import type { HisScore } from "../../api/scores";
import { HIS_AREAS } from "../../api/scores";
import type { MessageKey } from "../../i18n/en";
import { useT } from "../../i18n";
import { Card } from "../ui/Card";
import { DataState } from "../ui/DataState";
import { ScoreReadout } from "./ScoreReadout";

// The health score card of the overview (docs/design/mockups/src/Dashboard.dc.html): the value and
// each area's score, "n/a" with its reason where an area has none. The score is experimental (D9),
// so the card is the dashed experimental box. With no score, or `not_set`, there is no card.

const AREA_LABEL: Record<(typeof HIS_AREAS)[number], MessageKey> = {
  body: "overview.score.area.body",
  heart: "overview.score.area.heart",
  activity: "overview.score.area.activity",
  sleep: "overview.score.area.sleep",
};

export function ScoreCard({ score }: { score: HisScore | null }) {
  const t = useT();
  if (score === null || score.status === "not_set") return null;
  return (
    <Card level={3} experimental title={t("overview.metric.healthScore")}>
      {score.status === "computed" && score.value !== null ? (
        <>
          <p>
            <ScoreReadout value={score.value} />{" "}
            <span className="ov-small ov-muted">
              {t("overview.score.outOf")}
            </span>
          </p>
          <ul className="ov-small ov-muted">
            {HIS_AREAS.map((area) => (
              <li key={area}>
                {score.areas[area] === null
                  ? t("overview.score.areaNa", {
                      area: t(AREA_LABEL[area]),
                      reason: t("overview.score.reason.noInputs"),
                    })
                  : t("overview.score.areaValue", {
                      area: t(AREA_LABEL[area]),
                      value: String(score.areas[area]),
                    })}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <DataState kind="not-available">{t("overview.score.detail")}</DataState>
      )}
    </Card>
  );
}
