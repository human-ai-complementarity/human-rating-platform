import { useState, useEffect } from 'react';

import { withUtcSuffix } from '../time';

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
  const displaySeconds = inGrace ? graceRemaining : timeRemaining;
  const minutes = Math.floor(displaySeconds / 60);
  const seconds = displaySeconds % 60;

  const styles = {
    timer: {
      position: 'fixed' as const,
      top: 20,
      right: 20,
      background: isWarning || inGrace ? 'var(--danger)' : 'var(--ink)',
      color: '#fff',
      padding: '10px 16px',
      borderRadius: 'var(--radius-sm)',
      fontSize: 16,
      fontWeight: 700,
      fontFamily: 'var(--font-mono)',
      letterSpacing: '0.04em',
      boxShadow: '0 6px 18px rgba(40, 36, 32, 0.18)',
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      animation: isWarning || inGrace ? 'pulse 1s infinite' : 'none',
    },
    warning: {
      fontSize: 11,
      fontWeight: 500,
      opacity: 0.9,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.08em',
    },
  };

  return (
    <div style={styles.timer} data-testid={inGrace ? 'timer-grace' : 'timer'}>
      {String(minutes).padStart(2, '0')}:{String(seconds).padStart(2, '0')}
      {inGrace && <span style={styles.warning}>Finish this last one</span>}
      {isWarning && !inGrace && <span style={styles.warning}>Time running out!</span>}
    </div>
  );
}

export default Timer;
