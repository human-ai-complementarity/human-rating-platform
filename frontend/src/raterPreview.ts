// Builds the /rate URL that opens an experiment as a participant would see it.
//
// Prolific normally supplies PROLIFIC_PID / STUDY_ID / SESSION_ID; a preview
// fakes them. The PID is timestamped so each open is its own rater row rather
// than resuming (and resetting) the previous preview's ratings.

export function buildRaterPreviewUrl(experimentId: number, questionId?: number): string {
  const params = new URLSearchParams({
    experiment_id: String(experimentId),
    PROLIFIC_PID: `preview_${Date.now()}`,
    STUDY_ID: 'preview',
    SESSION_ID: 'preview',
    preview: 'true',
    // Pins the first question served, so an admin can jump straight to one
    // row from the analytics table instead of rating forward to reach it.
    ...(questionId !== undefined ? { question_id: String(questionId) } : {}),
  });
  return `${window.location.origin}/rate?${params.toString()}`;
}
