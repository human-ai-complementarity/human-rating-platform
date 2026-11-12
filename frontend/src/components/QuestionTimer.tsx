import { useState, useEffect } from 'react';

import TimerChip, { formatClock } from './TimerChip';

/** How long a rater should spend on one question at most. Advisory only:
 *  nothing is enforced, the rater is just told to move on. */
export const QUESTION_TIME_LIMIT_MINUTES = 20;

const STORAGE_KEY = 'hrp_question_started';

/** When the rater first saw this question. Kept in sessionStorage so a reload
 *  does not hand back a fresh twenty minutes. */
function loadStartedAt(questionId: number): number {
  const now = Date.now();
  try {
    const stored: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? 'null');
    if (
      typeof stored === 'object' &&
      stored !== null &&
      (stored as { id?: unknown }).id === questionId &&
      typeof (stored as { at?: unknown }).at === 'number'
    ) {
      return Math.min((stored as { at: number }).at, now);
    }
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id: questionId, at: now }));
  } catch {
    // Ignore storage failures (private mode, quota, etc.)
  }
  return now;
}

export function clearQuestionTimer() {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Ignore storage failures (private mode, quota, etc.)
  }
}

interface QuestionTimerProps {
  questionId: number;
  /** Fires on every change of the over-limit state, including back to false
   *  when the next question arrives. */
  onOverLimitChange: (overLimit: boolean) => void;
}

function QuestionTimer({ questionId, onOverLimitChange }: QuestionTimerProps) {
  const [elapsed, setElapsed] = useState<number | null>(null);

  const limitSeconds = QUESTION_TIME_LIMIT_MINUTES * 60;
  const overLimit = elapsed !== null && elapsed >= limitSeconds;

  useEffect(() => {
    const startedAt = loadStartedAt(questionId);
    const update = () => setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [questionId]);

  useEffect(() => {
    onOverLimitChange(overLimit);
  }, [overLimit, onOverLimitChange]);

  if (elapsed === null) return null;

  // Counts down to the limit, then counts up the time spent past it.
  return (
    <TimerChip
      label="Current question"
      restLabel="Question"
      time={`${overLimit ? '+' : ''}${formatClock(Math.abs(limitSeconds - elapsed))}`}
      alert={overLimit}
      note="Please move on"
      testId={overLimit ? 'question-timer-over' : 'question-timer'}
    />
  );
}

export default QuestionTimer;
