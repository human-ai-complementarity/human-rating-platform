import { useCallback, useState } from 'react';
import { pilotCheckRefusal } from '../../api';
import type { PilotCheckRefusal } from '../../types';
import ConfirmDialog from '../ConfirmDialog';
import { describeSummary } from './PilotCheckCard';

/**
 * The launch gate's client side (docs/pilot-check.md).
 *
 * Every study-creating call goes through `attempt`. When the backend answers
 * with the pilot check's 409, the refusal is shown in a dialog built from the
 * 409's own body, so the UI and the API cannot disagree about why. "Launch
 * anyway" repeats the call with the acknowledgement flag; in block mode the
 * dialog has no such button. Any other error is rethrown to the caller.
 */

type Launch = (acknowledgePilotCheck: boolean) => Promise<void>;

export function usePilotCheckGate() {
  const [pending, setPending] = useState<{ refusal: PilotCheckRefusal; launch: Launch } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  // Resolves true when the launch ran, false when it was deferred to the dialog.
  const attempt = useCallback(async (launch: Launch): Promise<boolean> => {
    try {
      await launch(false);
      return true;
    } catch (err) {
      const refusal = pilotCheckRefusal(err);
      if (!refusal) throw err;
      setRetryError(null);
      setPending({ refusal, launch });
      return false;
    }
  }, []);

  const close = useCallback(() => {
    if (busy) return;
    setPending(null);
    setRetryError(null);
  }, [busy]);

  const confirm = useCallback(async () => {
    if (!pending) return;
    if (pending.refusal.code === 'pilot_check_blocked') {
      close();
      return;
    }
    setBusy(true);
    try {
      await pending.launch(true);
      setPending(null);
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : 'Failed to launch');
    } finally {
      setBusy(false);
    }
  }, [pending, close]);

  const dialog = pending ? (
    <div data-testid="pilot-check-dialog">
      <ConfirmDialog
        title={
          pending.refusal.code === 'pilot_check_blocked'
            ? 'Pilot check required'
            : 'Launch without a clean pilot check?'
        }
        message={
          <>
            <div>{describeSummary(pending.refusal.pilot_check)}</div>
            <div style={{ marginTop: 8 }}>
              {pending.refusal.code === 'pilot_check_blocked'
                ? 'Run the pilot check from the card above and fix what it finds before creating a study.'
                : 'Run the pilot check from the card above first, or launch anyway and it will be recorded on the round.'}
            </div>
            {retryError && (
              <div role="alert" style={{ marginTop: 8, color: 'var(--danger)' }}>
                {retryError}
              </div>
            )}
          </>
        }
        confirmLabel={pending.refusal.code === 'pilot_check_blocked' ? 'OK' : 'Launch anyway'}
        cancelLabel={pending.refusal.code === 'pilot_check_blocked' ? 'Close' : 'Run the check first'}
        tone={pending.refusal.code === 'pilot_check_blocked' ? 'default' : 'danger'}
        busy={busy}
        onConfirm={confirm}
        onCancel={close}
      />
    </div>
  ) : null;

  return { attempt, dialog };
}
