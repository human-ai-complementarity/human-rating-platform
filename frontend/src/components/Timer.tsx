import { useState, useEffect } from 'react';

import { withUtcSuffix } from '../time';
import TimerChip, { formatClock } from './TimerChip';

interface TimerProps {
  sessionEndTime: string;
  /** Extra seconds past sessionEndTime in which the open question may still be
   *  submitted. New questions stop at sessionEndTime either way. */
  graceSeconds?: number;
  /** The clock ran out: stop asking for new questions, let the rater finish. */
  onDeadline: () => void;
  /** Even the grace window is gone: nothing more can be submitted. */
  onExpire: () => void;
}

function Timer({ sessionEndTime, graceSeconds = 0, onDeadline, onExpire }: TimerProps) {
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);
  const [graceRemaining, setGraceRemaining] = useState(0);
  const [isWarning, setIsWarning] = useState(false);

  useEffect(() => {
    const endTime = new Date(withUtcSuffix(sessionEndTime)).getTime();
    const hardEndTime = endTime + graceSeconds * 1000;

    // Fire each transition once. Without this the callbacks run on every tick
    // past the threshold, which is harmless while they only set a flag but
    // turns into a request storm the moment either does real work.
    let firedDeadline = false;
    let firedExpiry = false;

    const updateTimer = () => {
      const now = Date.now();
      const remaining = Math.max(0, Math.floor((endTime - now) / 1000));
      const graceLeft = Math.max(0, Math.floor((hardEndTime - now) / 1000));

      setTimeRemaining(remaining);
      setGraceRemaining(graceLeft);
      setIsWarning(remaining <= 300);

      if (remaining <= 0 && !firedDeadline) {
        firedDeadline = true;
        onDeadline();
      }
      if (graceLeft <= 0 && !firedExpiry) {
        firedExpiry = true;
        onExpire();
      }
    };

    updateTimer();
    const interval = setInterval(updateTimer, 1000);

    return () => clearInterval(interval);
  }, [sessionEndTime, graceSeconds, onDeadline, onExpire]);

  if (timeRemaining === null) return null;

  const inGrace = timeRemaining <= 0 && graceRemaining > 0;

  return (
    <TimerChip
      label="Session"
      time={formatClock(inGrace ? graceRemaining : timeRemaining)}
      alert={isWarning || inGrace}
      note={inGrace ? 'Finish this last one' : 'Time running out!'}
      testId={inGrace ? 'timer-grace' : 'timer'}
    />
  );
}

export default Timer;
