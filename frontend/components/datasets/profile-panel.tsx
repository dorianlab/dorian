"use client";

import { Badge } from "@/components/ui/badge";
import type { AvailableDataset } from "@/types/dataset";

interface Props {
  dataset: AvailableDataset;
}

/** Well-known metafeature groups for nicer display. */
const GROUPS: Record<string, string[]> = {
  "Shape": [
    "NumberOfInstances",
    "NumberOfFeatures",
    "NumberOfClasses",
    "NumberOfNumericFeatures",
    "NumberOfSymbolicFeatures",
    "Dimensionality",
    "PercentageOfNumericFeatures",
    "PercentageOfSymbolicFeatures",
  ],
  "Class Balance": [
    "MajorityClassSize",
    "MinorityClassSize",
    "MajorityClassPercentage",
    "MinorityClassPercentage",
    "ClassEntropy",
  ],
  "Missing Values": [
    "NumberOfMissingValues",
    "PercentageOfMissingValues",
    "NumberOfInstancesWithMissingValues",
    "PercentageOfInstancesWithMissingValues",
    "NumberOfFeaturesWithMissingValues",
    "PercentageOfFeaturesWithMissingValues",
  ],
  "Statistics": [
    "MeanMeansOfNumericAtts",
    "MeanStdDevOfNumericAtts",
    "MeanKurtosisOfNumericAtts",
    "MeanSkewnessOfNumericAtts",
  ],
};

const ALL_GROUPED = new Set(Object.values(GROUPS).flat());

export function ProfilePanel({ dataset }: Props) {
  const profile = dataset.profile;
  if (!profile || Object.keys(profile).length === 0) {
    return (
      <div className="text-center py-12 text-sm text-muted-foreground">
        No profile data available. The dataset has not been profiled yet.
      </div>
    );
  }

  // Profiling errors (captured during metafeature computation)
  const profileErrors: Record<string, string> =
    (profile as Record<string, unknown>)["__errors__"] as Record<string, string> ?? {};

  // Collect ungrouped keys (exclude __errors__ meta-key)
  const ungrouped = Object.keys(profile).filter((k) => !ALL_GROUPED.has(k) && k !== "__errors__");

  return (
    <div className="space-y-6">
      {/* Feature / target columns */}
      {dataset.features && dataset.features.length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-2">Feature columns</h3>
          <div className="flex flex-wrap gap-1.5">
            {dataset.features.map((f) => (
              <Badge key={f} variant="secondary" className="text-[11px]">
                {f}
              </Badge>
            ))}
          </div>
        </div>
      )}
      {dataset.targets && dataset.targets.length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-2">Target columns</h3>
          <div className="flex flex-wrap gap-1.5">
            {dataset.targets.map((t) => (
              <Badge key={t} variant="outline" className="text-[11px]">
                {t}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {/* Grouped metafeatures */}
      {Object.entries(GROUPS).map(([group, keys]) => {
        const present = keys.filter((k) => k in profile);
        if (present.length === 0) return null;
        return (
          <div key={group}>
            <h3 className="text-sm font-medium mb-2">{group}</h3>
            <MetafeatureGrid profile={profile} keys={present} />
          </div>
        );
      })}

      {/* Ungrouped */}
      {ungrouped.length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-2">Other metafeatures</h3>
          <MetafeatureGrid profile={profile} keys={ungrouped} />
        </div>
      )}

      {/* Profiling errors */}
      {Object.keys(profileErrors).length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-2 text-amber-600">
            Failed metafeatures ({Object.keys(profileErrors).length})
          </h3>
          <div className="space-y-1.5">
            {Object.entries(profileErrors).map(([name, error]) => (
              <div
                key={name}
                className="rounded border border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/30 px-3 py-1.5 text-xs"
              >
                <span className="font-medium">{humanize(name)}:</span>{" "}
                <span className="text-muted-foreground">{error}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function MetafeatureGrid({
  profile,
  keys,
}: {
  profile: Record<string, number>;
  keys: string[];
}) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
      {keys.map((key) => {
        const val = profile[key];
        const display =
          val == null
            ? "-"
            : Number.isInteger(val)
              ? val.toLocaleString()
              : val.toFixed(4);
        return (
          <div
            key={key}
            className="rounded-lg border border-border px-3 py-2 space-y-0.5"
          >
            <div className="text-[11px] text-muted-foreground truncate" title={key}>
              {humanize(key)}
            </div>
            <div className="text-sm font-mono font-medium">{display}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Convert PascalCase metafeature name to human-readable label. */
function humanize(name: string): string {
  return name
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .replace(/Of /g, "of ");
}
