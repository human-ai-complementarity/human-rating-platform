import { useEffect, useState } from 'react';
import { api } from '../api';
import type { AccessRosterEntry } from '../types';
import { Banner } from './experiment-detail/ui';

/**
 * Who can use the admin surface, as last synced from the team Google Group.
 * Read-only on purpose: the group is the only place membership changes, and
 * this page exists so admins can see the list without opening Google.
 */
function TeamPage() {
  const [entries, setEntries] = useState<AccessRosterEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setEntries(await api.listAccessRoster());
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const admins = entries.filter((e) => e.role === 'admin');
  const members = entries.filter((e) => e.role === 'member');
  const syncedAt = entries[0]?.synced_at ? new Date(entries[0].synced_at).toLocaleString() : null;

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
          Team
        </h1>
        <p style={{ margin: '8px 0 0', color: 'var(--ink-muted)', maxWidth: 640 }}>
          Everyone here can sign in. The list is synced from the team Google Group every few
          minutes; to add or remove someone, change the group.
          {syncedAt ? ` Last synced ${syncedAt}.` : ''}
        </p>
      </div>

      {error && <Banner tone="danger">{error}</Banner>}
      {loading && <p style={{ color: 'var(--ink-muted)' }}>Loading…</p>}
      {!loading && !error && entries.length === 0 && (
        <Banner tone="warn">No one has been synced yet. Run the access sync once, then reload.</Banner>
      )}

      {!loading && entries.length > 0 && (
        <div style={{ display: 'grid', gap: 24, gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
          <RosterList title="Admins" hint="Group owners and managers" entries={admins} />
          <RosterList title="Members" hint="Everyone else in the group" entries={members} />
        </div>
      )}
    </div>
  );
}

function RosterList({ title, hint, entries }: { title: string; hint: string; entries: AccessRosterEntry[] }) {
  return (
    <section
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--line)',
        borderRadius: 10,
        padding: '16px 20px',
      }}
    >
      <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>
        {title} <span style={{ color: 'var(--ink-muted)', fontWeight: 400 }}>({entries.length})</span>
      </h2>
      <p style={{ margin: '4px 0 12px', color: 'var(--ink-muted)', fontSize: 13 }}>{hint}</p>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {entries.map((entry) => (
          <li
            key={entry.email}
            style={{ padding: '6px 0', borderTop: '1px solid var(--line)', fontFamily: 'var(--font-mono, monospace)', fontSize: 13 }}
          >
            {entry.email}
          </li>
        ))}
        {entries.length === 0 && <li style={{ color: 'var(--ink-muted)', fontSize: 13 }}>None</li>}
      </ul>
    </section>
  );
}

export default TeamPage;
