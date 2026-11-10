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
 * anyway" repeats the call with the acknowledgement flag; the check can ask,
 * never refuse. Any other error is rethrown to the caller.
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
    setBusy(true);
    try {
      await pending.launch(true);
      setPending(null);
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : 'Failed to launch');
    } finally {
      setBusy(false);
    }
  }, [pending]);

  const dialog = pending ? (
    <div data-testid="pilot-check-dialog">
      <ConfirmDialog
        title="Launch without a clean pilot check?"
        message={
          <>
            <div>{describeSummary(pending.refusal.pilot_check)}</div>
            <div style={{ marginTop: 8 }}>
              Run the pilot check from the card above first, or launch anyway. You will not be
              asked again for this run unless the experiment changes.
            </div>
            {retryError && (
              <div role="alert" style={{ marginTop: 8, color: 'var(--danger)' }}>
                {retryError}
              </div>
            )}
          </>
        }
        confirmLabel="Launch anyway"
        cancelLabel="Run the check first"
        tone="danger"
        busy={busy}
        onConfirm={confirm}
        onCancel={close}
      />
    </div>
  ) : null;

  return { attempt, dialog };
}
