import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import type { ApiKey, ApiKeyCreated } from '../types';
import ConfirmDialog from './ConfirmDialog';
import {
  Banner,
  Field,
  inputStyle,
  primaryButton,
  secondaryButton,
  SectionCard,
  Toast,
} from './experiment-detail/ui';

/**
 * Manage bearer keys for the programmatic /api/v1 read API. Keys are minted
 * here (the raw secret is shown exactly once), then can be regenerated
 * (rotate the secret under the same name) or revoked. Mirrors the Experiments
 * tab: a create panel on the left, the existing keys on the right.
 */
// Interactive API reference. In prod the docs are served from the API host
// (VITE_API_HOST); in local dev VITE_API_HOST is empty and the relative /docs
// path is proxied to the backend by Vite.
const DOCS_URL = `${import.meta.env.VITE_API_HOST || ''}/docs`;

function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The one-time plaintext reveal after a create/regenerate. Cleared when the
  // user dismisses it — it can never be shown again.
  const [revealed, setRevealed] = useState<{ key: ApiKeyCreated; action: 'created' | 'regenerated' } | null>(null);

  // Confirmations for the two state-changing actions on an existing key.
  const [pendingRevoke, setPendingRevoke] = useState<ApiKey | null>(null);
  const [pendingRegenerate, setPendingRegenerate] = useState<ApiKey | null>(null);
  const [busyAction, setBusyAction] = useState(false);

  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    void loadKeys();
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  const loadKeys = async () => {
    try {
      setLoading(true);
      setKeys(await api.listApiKeys());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  const flash = (message: string) => {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  };

  const handleCreate = async (name: string) => {
    setError(null);
    const created = await api.createApiKey(name);
    setRevealed({ key: created, action: 'created' });
    await loadKeys();
  };

  const runRegenerate = async () => {
    if (!pendingRegenerate) return;
    setBusyAction(true);
    setError(null);
    try {
      const regenerated = await api.regenerateApiKey(pendingRegenerate.id);
      setPendingRegenerate(null);
      setRevealed({ key: regenerated, action: 'regenerated' });
      await loadKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      setPendingRegenerate(null);
    } finally {
      setBusyAction(false);
    }
  };

  const runRevoke = async () => {
    if (!pendingRevoke) return;
    setBusyAction(true);
    setError(null);
    try {
      await api.revokeApiKey(pendingRevoke.id);
      flash(`Revoked “${pendingRevoke.name}”`);
      setPendingRevoke(null);
      await loadKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      setPendingRevoke(null);
    } finally {
      setBusyAction(false);
    }
  };

  return (
    <div className="admin-page">
      <div style={{ marginBottom: 28 }}>
        <h1
          style={{
            fontFamily: 'var(--font-head)',
            fontWeight: 600,
            fontSize: 30,
            letterSpacing: '-0.01em',
            margin: 0,
          }}
        >
          API Keys
        </h1>
        <p style={{ margin: '6px 0 0', fontSize: 15, color: 'var(--muted)' }}>
          Bearer keys for the programmatic <code>/api/v1</code> read API — used by CLIs and
          inference pipelines to fetch experiment data. See the{' '}
          <a
            href={DOCS_URL}
            target="_blank"
            rel="noreferrer"
            style={{ color: 'var(--accent)', fontWeight: 600 }}
          >
            API docs
          </a>{' '}
          for the endpoints and how to authenticate.
        </p>
      </div>

      {error && (
        <div style={{ marginBottom: 20 }}>
          <Banner tone="danger">{error}</Banner>
        </div>
      )}

      {revealed && (
        <div style={{ marginBottom: 20 }}>
          <RevealCard
            created={revealed.key}
            action={revealed.action}
            onDismiss={() => setRevealed(null)}
            onCopied={() => flash('Key copied to clipboard')}
          />
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '410px 1fr', gap: 28, alignItems: 'start' }}>
        <CreatePanel onCreate={handleCreate} onError={setError} />
        <KeyListPanel
          keys={keys}
          loading={loading}
          onRegenerate={(k) => setPendingRegenerate(k)}
          onRevoke={(k) => setPendingRevoke(k)}
        />
      </div>

      {pendingRegenerate && (
        <ConfirmDialog
          title="Regenerate key"
          message={
            <>
              Regenerate <strong>{pendingRegenerate.name}</strong>? The current secret stops
              working immediately and any client using it must switch to the new one. This cannot be
              undone.
            </>
          }
          confirmLabel="Regenerate"
          busy={busyAction}
          onConfirm={runRegenerate}
          onCancel={() => setPendingRegenerate(null)}
        />
      )}

      {pendingRevoke && (
        <ConfirmDialog
          title="Revoke key"
          message={
            <>
              Revoke <strong>{pendingRevoke.name}</strong>? Any client using it will start getting
              401s immediately. You can regenerate it later to issue a fresh secret under the same
              name.
            </>
          }
          confirmLabel="Revoke"
          tone="danger"
          busy={busyAction}
          onConfirm={runRevoke}
          onCancel={() => setPendingRevoke(null)}
        />
      )}

      {toast && (
        <div style={{ position: 'fixed', bottom: 28, left: '50%', transform: 'translateX(-50%)', zIndex: 50, minWidth: 260 }}>
          <Toast tone="ok" onDismiss={() => setToast(null)}>
            {toast}
          </Toast>
        </div>
      )}
    </div>
  );
}

function CreatePanel({
  onCreate,
  onError,
}: {
  onCreate: (name: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      await onCreate(trimmed);
      setName('');
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <SectionCard header="Create new">
      <form onSubmit={submit}>
        <Field
          id="api-key-name"
          label="Name"
          hint="A label so you can recognize this key later, e.g. “inference-pipeline”."
        >
          <input
            id="api-key-name"
            data-testid="api-key-name-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., inference-pipeline"
            style={inputStyle}
            required
          />
        </Field>
        <div style={{ marginBottom: 18 }}>
          <Banner tone="info">
            The full key is shown only once, right after you create it. Store it somewhere safe.
          </Banner>
        </div>
        <button
          type="submit"
          data-testid="api-key-create-button"
          disabled={submitting || name.trim() === ''}
          style={{
            ...primaryButton,
            width: '100%',
            padding: 13,
            fontSize: 15,
            opacity: submitting || name.trim() === '' ? 0.6 : 1,
          }}
        >
          {submitting ? 'Creating…' : 'Create key'}
        </button>
      </form>
    </SectionCard>
  );
}

function RevealCard({
  created,
  action,
  onDismiss,
  onCopied,
}: {
  created: ApiKeyCreated;
  action: 'created' | 'regenerated';
  onDismiss: () => void;
  onCopied: () => void;
}) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(created.plaintext_key);
      onCopied();
    } catch {
      // Clipboard can be blocked (permissions/non-secure context); the key is
      // still visible for manual copy, so this is a non-fatal best-effort.
    }
  };

  return (
    <section
      data-testid="api-key-reveal"
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--accent)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
        padding: 22,
      }}
    >
      <div
        style={{
          font: '600 11px/1 var(--font-mono)',
          letterSpacing: '0.16em',
          textTransform: 'uppercase',
          color: 'var(--accent-soft-ink)',
          marginBottom: 10,
        }}
      >
        Key {action} — copy it now
      </div>
      <p style={{ margin: '0 0 14px', fontSize: 13.5, color: 'var(--muted)', lineHeight: 1.55 }}>
        This is the only time <strong>{created.name}</strong> will be shown in full. Once you dismiss
        this, only its prefix remains visible.
      </p>
      <div style={{ display: 'flex', gap: 10, alignItems: 'stretch' }}>
        <code
          data-testid="api-key-plaintext"
          style={{
            flex: 1,
            padding: '12px 14px',
            background: 'var(--surface-2)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            font: '500 13.5px var(--font-mono)',
            color: 'var(--ink)',
            wordBreak: 'break-all',
            userSelect: 'all',
          }}
        >
          {created.plaintext_key}
        </code>
        <button type="button" onClick={copy} style={{ ...primaryButton, whiteSpace: 'nowrap' }}>
          Copy
        </button>
      </div>
      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
        <button type="button" onClick={onDismiss} style={secondaryButton}>
          Done
        </button>
      </div>
    </section>
  );
}

function KeyListPanel({
  keys,
  loading,
  onRegenerate,
  onRevoke,
}: {
  keys: ApiKey[];
  loading: boolean;
  onRegenerate: (key: ApiKey) => void;
  onRevoke: (key: ApiKey) => void;
}) {
  const activeCount = useMemo(() => keys.filter((k) => k.is_active).length, [keys]);

  return (
    <section
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '18px 24px',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <div
          style={{
            font: '600 11px/1 var(--font-mono)',
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: 'var(--muted)',
          }}
        >
          Your keys
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)' }}>
          <span style={{ fontWeight: 700, color: 'var(--ink)' }}>{activeCount}</span> active
        </div>
      </div>

      <div>
        {loading ? (
          <EmptyState text="Loading…" />
        ) : keys.length === 0 ? (
          <EmptyState text="No API keys yet. Create one to start using the /api/v1 endpoints." />
        ) : (
          keys.map((key, idx) => (
            <KeyRow
              key={key.id}
              apiKey={key}
              isLast={idx === keys.length - 1}
              onRegenerate={() => onRegenerate(key)}
              onRevoke={() => onRevoke(key)}
            />
          ))
        )}
      </div>
    </section>
  );
}

function formatDate(value: string | null): string {
  if (!value) return 'Never';
  const d = new Date(value);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function KeyRow({
  apiKey,
  isLast,
  onRegenerate,
  onRevoke,
}: {
  apiKey: ApiKey;
  isLast: boolean;
  onRegenerate: () => void;
  onRevoke: () => void;
}) {
  return (
    <div
      data-testid="api-key-row"
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 20,
        padding: '18px 24px',
        borderBottom: isLast ? 'none' : '1px solid var(--line)',
        opacity: apiKey.is_active ? 1 : 0.62,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, flexWrap: 'wrap' }}>
          <span style={{ fontFamily: 'var(--font-head)', fontSize: 17, fontWeight: 600, letterSpacing: '-0.01em' }}>
            {apiKey.name}
          </span>
          {apiKey.is_active ? <ActivePill /> : <RevokedPill />}
        </div>
        <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
          <code style={{ font: '500 12.5px var(--font-mono)', color: 'var(--muted)' }}>
            {apiKey.masked_key}
          </code>
          <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>
            Created {formatDate(apiKey.created_at)}
            {apiKey.created_by ? ` by ${apiKey.created_by}` : ''} · Last used{' '}
            {formatDate(apiKey.last_used_at)}
          </span>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
        <button
          type="button"
          data-testid="api-key-regenerate"
          onClick={onRegenerate}
          style={{ ...secondaryButton, padding: '8px 14px', font: '600 13px var(--font-body)' }}
        >
          Regenerate
        </button>
        {apiKey.is_active && (
          <button
            type="button"
            data-testid="api-key-revoke"
            onClick={onRevoke}
            style={{
              padding: '8px 14px',
              background: 'var(--surface)',
              color: 'var(--danger)',
              border: '1px solid var(--danger)',
              borderRadius: 'var(--radius-sm)',
              font: '600 13px var(--font-body)',
              cursor: 'pointer',
            }}
          >
            Revoke
          </button>
        )}
      </div>
    </div>
  );
}

function ActivePill() {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        padding: '3px 9px',
        borderRadius: 99,
        background: 'var(--ok-soft)',
        color: 'var(--accent-soft-ink)',
        font: '600 11px var(--font-body)',
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)' }} />
      Active
    </span>
  );
}

function RevokedPill() {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '3px 9px',
        borderRadius: 99,
        background: 'var(--surface-2)',
        color: 'var(--muted)',
        font: '600 11px var(--font-body)',
      }}
    >
      Revoked
    </span>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div style={{ padding: '52px 20px', textAlign: 'center', color: 'var(--muted)', fontSize: 14 }}>
      {text}
    </div>
  );
}

export default ApiKeysPage;
