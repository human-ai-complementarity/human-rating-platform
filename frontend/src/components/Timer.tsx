import { useState, useEffect } from 'react';

interface TimerProps {
  sessionEndTime: string;
  onExpire: () => void;
}

function Timer({ sessionEndTime, onExpire }: TimerProps) {
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);
  const [isWarning, setIsWarning] = useState(false);

  useEffect(() => {
    const utcTimeString = sessionEndTime.endsWith('Z') ? sessionEndTime : sessionEndTime + 'Z';
    const endTime = new Date(utcTimeString).getTime();

    const updateTimer = () => {
      const now = Date.now();
      const remaining = Math.max(0, Math.floor((endTime - now) / 1000));

      setTimeRemaining(remaining);
      setIsWarning(remaining <= 300);

      if (remaining <= 0) {
        onExpire();
      }
    };

    updateTimer();
    const interval = setInterval(updateTimer, 1000);

    return () => clearInterval(interval);
  }, [sessionEndTime, onExpire]);

  if (timeRemaining === null) return null;

  const minutes = Math.floor(timeRemaining / 60);
  const seconds = timeRemaining % 60;

  const styles = {
    timer: {
      position: 'fixed' as const,
      top: 20,
      right: 20,
      background: isWarning ? 'var(--danger)' : 'var(--ink)',
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
      animation: isWarning ? 'pulse 1s infinite' : 'none',
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
    <div style={styles.timer}>
      {String(minutes).padStart(2, '0')}:{String(seconds).padStart(2, '0')}
      {isWarning && <span style={styles.warning}>Time running out!</span>}
    </div>
  );
}

export default Timer;
