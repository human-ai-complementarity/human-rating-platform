interface TimerChipProps {
  label: string;
  /** Shorter label for the panel's collapsed (at rest) state. */
  restLabel?: string;
  /** Leave this row out of the collapsed panel. */
  hideAtRest?: boolean;
  /** Already formatted, e.g. "19:48", "+01:23", or a plain count. */
  time: string;
  /** Red and pulsing: the rater should act on this clock now. */
  alert?: boolean;
  /** What to do about it, shown only while alerting. */
  note?: string;
  testId: string;
}

export function formatClock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

/** One row of the floating rater status panel: label on the left, value on the
 *  right, so stacked rows line up as a single readout. */
function TimerChip({
  label,
  restLabel,
  hideAtRest = false,
  time,
  alert = false,
  note,
  testId,
}: TimerChipProps) {
  const styles = {
    row: {
      background: alert ? 'var(--danger)' : 'transparent',
      borderRadius: 'calc(var(--radius-sm) - 2px)',
      padding: '5px 10px',
      animation: alert ? 'pulse 1s infinite' : 'none',
    },
    line: {
      display: 'flex',
      alignItems: 'baseline',
      justifyContent: 'space-between',
      gap: 18,
    },
    label: {
      fontSize: 'var(--text-label)',
      fontWeight: 500,
      opacity: alert ? 1 : 0.7,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.08em',
    },
    value: {
      fontSize: 'var(--text-secondary)',
      fontWeight: 700,
      fontVariantNumeric: 'tabular-nums',
      letterSpacing: '0.04em',
    },
    note: {
      fontSize: 'var(--text-label)',
      fontWeight: 700,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.08em',
      marginTop: 2,
    },
  };

  return (
    <div
      style={styles.row}
      data-testid={testId}
      data-alert={alert}
      data-hide-at-rest={hideAtRest}
    >
      <div style={styles.line}>
        <span style={styles.label}>
          {restLabel ? (
            <>
              <span className="rsp-rest">{restLabel}</span>
              <span className="rsp-full">{label}</span>
            </>
          ) : (
            label
          )}
        </span>
        <span style={styles.value}>{time}</span>
      </div>
      {alert && note && <div style={styles.note}>{note}</div>}
    </div>
  );
}

export default TimerChip;
