import clsx from 'clsx';

export interface IntakeState {
  stage: string;
  caller: { name: string; dob: string; new_patient: boolean } | null;
  callback_number: { number: string; confirmed_by_caller: boolean; source: string } | null;
  intake: Record<string, any>;
  priority: { level: string; window: string; reasons: string[]; needs_nurse_review: boolean } | null;
  cleared_safety_checks: string[];
  pending_safety_check: string | null;
  booking: Record<string, any> | null;
  emergency: { label: string; category: string; source: string } | null;
  handoff: Record<string, any> | null;
  events: { at: string; kind: string; detail: string }[];
  demo: { fail_next_tool: boolean; slot_conflict: boolean };
}

const STAGE_STYLES: Record<string, string> = {
  identify: 'bg-gray-100 text-gray-700',
  intake: 'bg-blue-100 text-blue-800',
  schedule: 'bg-indigo-100 text-indigo-800',
  booked: 'bg-green-100 text-green-800',
  emergency: 'bg-red-100 text-red-800',
  handoff: 'bg-amber-100 text-amber-800',
};

const PRIORITY_STYLES: Record<string, string> = {
  same_day: 'bg-orange-100 text-orange-800',
  soon: 'bg-yellow-100 text-yellow-800',
  routine: 'bg-green-100 text-green-800',
};

const EVENT_STYLES: Record<string, string> = {
  emergency: 'text-red-700',
  escalation: 'text-red-700',
  safety_check: 'text-orange-700',
  handoff: 'text-amber-700',
  tool_failure: 'text-red-600',
  conflict: 'text-amber-700',
  booked: 'text-green-700',
};

interface StaffPanelProps {
  state: IntakeState | null;
  lastLatencyMs: number | null;
  onToggleDemo: (key: 'fail_next_tool' | 'slot_conflict', value: boolean) => void;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-sm">
      <span className="w-24 shrink-0 text-gray-500">{label}</span>
      <span className="min-w-0 flex-1 break-words text-gray-900">{children}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-gray-100 px-4 py-3">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">{title}</h3>
      <div className="space-y-1.5">{children}</div>
    </section>
  );
}

export function StaffPanel({ state, lastLatencyMs, onToggleDemo }: StaffPanelProps) {
  if (!state) {
    return <div className="card p-4 text-sm text-gray-500">Waiting for the server…</div>;
  }
  const { caller, callback_number: callback, priority, booking, emergency, handoff } = state;

  return (
    <aside className="card overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3">
        <h2 className="text-sm font-semibold">Staff view</h2>
        <span className={clsx('chip', STAGE_STYLES[state.stage])}>{state.stage}</span>
      </div>

      {emergency && (
        <div className="mx-4 mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          <strong>Emergency:</strong> {emergency.label} ({emergency.source}). Booking stopped; 911 advised.
        </div>
      )}

      <Section title="Caller">
        <Row label="Name">{caller ? `${caller.name}${caller.new_patient ? ' (new)' : ''}` : '—'}</Row>
        <Row label="DOB">{caller?.dob || '—'}</Row>
        <Row label="Callback">
          {callback ? (
            <>
              {callback.number}{' '}
              <span className={clsx('chip', callback.confirmed_by_caller ? 'bg-green-100 text-green-800' : '')}>
                {callback.confirmed_by_caller ? 'confirmed' : `from ${callback.source}, unconfirmed`}
              </span>
            </>
          ) : (
            '—'
          )}
        </Row>
        <Row label="Reason">{state.intake?.reason_for_visit || '—'}</Row>
      </Section>

      <Section title="Priority">
        {priority ? (
          <>
            <div className="flex items-center gap-2">
              <span className={clsx('chip', PRIORITY_STYLES[priority.level])}>{priority.level.replace('_', '-')}</span>
              <span className="text-sm text-gray-600">{priority.window}</span>
              {priority.needs_nurse_review && <span className="chip bg-amber-100 text-amber-800">nurse review</span>}
            </div>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-gray-600">
              {priority.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </>
        ) : (
          <p className="text-sm text-gray-500">Not assigned yet</p>
        )}
      </Section>

      {(state.pending_safety_check || state.cleared_safety_checks.length > 0) && (
        <Section title="Safety checks">
          {state.pending_safety_check && <Row label="Asking">{state.pending_safety_check.replace('_', ' ')}</Row>}
          {state.cleared_safety_checks.length > 0 && (
            <Row label="Ruled out">{state.cleared_safety_checks.map((c) => c.replace('_', ' ')).join(', ')}</Row>
          )}
        </Section>
      )}

      {booking && (
        <Section title="Booking">
          <Row label="When">{booking.when}</Row>
          <Row label="Confirmation">{booking.confirmation}</Row>
        </Section>
      )}

      {handoff && (
        <Section title={`Handoff → ${handoff.queue}`}>
          <Row label="Why">{handoff.reason.replace('_', ' ')}</Row>
          <Row label="Note">{handoff.note}</Row>
          <Row label="Caller">{handoff.caller}</Row>
          <Row label="Callback">{handoff.callback_number}</Row>
          <Row label="Reason">{handoff.reason_for_visit}</Row>
          <Row label="Priority">{handoff.priority?.level ?? 'not assessed'}</Row>
        </Section>
      )}

      <Section title="Event log">
        {state.events.length === 0 && <p className="text-sm text-gray-500">No events yet</p>}
        <ol className="max-h-48 space-y-1 overflow-y-auto font-mono text-[11px] leading-snug">
          {state.events.map((event, index) => (
            <li key={index} className={EVENT_STYLES[event.kind] || 'text-gray-600'}>
              {event.at} {event.kind}: {event.detail}
            </li>
          ))}
        </ol>
      </Section>

      <Section title="Demo controls">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={state.demo.fail_next_tool}
            onChange={(e) => onToggleDemo('fail_next_tool', e.target.checked)}
          />
          Fail next system call
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={state.demo.slot_conflict}
            onChange={(e) => onToggleDemo('slot_conflict', e.target.checked)}
          />
          Slot taken at booking
        </label>
        {lastLatencyMs !== null && <p className="text-xs text-gray-500">Last turn: {lastLatencyMs} ms</p>}
      </Section>
    </aside>
  );
}
