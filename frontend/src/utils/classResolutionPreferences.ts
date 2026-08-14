import type {
  ClassNameConflict,
  ClassResolutionAction,
  ClassResolutionChoice,
  ClassResolutionPlan,
} from "../api/client";

export type ClassResolutionPreferences = Record<string, ClassResolutionAction>;

function conflictSignature(conflict: ClassNameConflict) {
  return JSON.stringify([
    conflict.class_id,
    conflict.project_name,
    conflict.uploaded_name,
  ]);
}

export function choicesWithRememberedPreferences(
  plan: ClassResolutionPlan,
  preferences: ClassResolutionPreferences,
) {
  return Object.fromEntries(plan.conflicts.map((conflict) => [
    conflict.key,
    preferences[conflictSignature(conflict)] ?? "use_project",
  ])) as Record<string, ClassResolutionAction>;
}

export function resolutionsFromPreferences(
  plan: ClassResolutionPlan,
  preferences: ClassResolutionPreferences,
): ClassResolutionChoice[] | null {
  const resolutions: ClassResolutionChoice[] = [];
  for (const conflict of plan.conflicts) {
    const action = preferences[conflictSignature(conflict)];
    if (action === undefined) return null;
    resolutions.push({ key: conflict.key, action });
  }
  return resolutions;
}

export function rememberClassResolutions(
  preferences: ClassResolutionPreferences,
  plan: ClassResolutionPlan,
  resolutions: readonly ClassResolutionChoice[],
) {
  const actionByKey = new Map(
    resolutions.map((resolution) => [resolution.key, resolution.action]),
  );
  const next = { ...preferences };
  plan.conflicts.forEach((conflict) => {
    const action = actionByKey.get(conflict.key);
    if (action !== undefined) next[conflictSignature(conflict)] = action;
  });
  return next;
}
