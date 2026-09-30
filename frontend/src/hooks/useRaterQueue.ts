import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import type { AssistanceStep, Question, QueueSnapshot, RatingSubmit, Session } from '../types';

type Answers = Record<number, { answer: string; confidence: number }>;
export interface AssistanceResource {
  step: AssistanceStep | null;
  loading: boolean;
  error: string | null;
  advance: (answers: Answers) => Promise<void>;
  retry: () => Promise<void>;
}
const empty = { question: null as Question | null, step: null as AssistanceStep | null, loading: false, error: null as string | null };
const message = (error: unknown) => error instanceof Error ? error.message : 'Request failed';

/** Owns queue identity, first-step loading, and acceptance of async results. */
export function useRaterQueue() {
  const [resource, setResource] = useState(empty);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [submissionConflict, setSubmissionConflict] = useState(false);
  const [submissionPending, setSubmissionPending] = useState(false);
  const session = useRef<Session | null>(null);
  const epoch = useRef(0);
  const queue = useRef<QueueSnapshot | null>(null);
  const current = useRef<Question | null>(null);
  const currentStep = useRef<AssistanceStep | null>(null);
  const refill = useRef<Promise<QueueSnapshot> | null>(null);
  const refillAgain = useRef(false);
  const prepared = useRef(new Set<string>());
  const frozenSubmit = useRef<RatingSubmit | null>(null);
  const submitting = useRef(false);
  const advancing = useRef(false);
  const frozenAdvance = useRef<{ sessionId: number; revision: number; answers: Answers } | null>(null);

  const configure = useCallback((value: Session) => {
    epoch.current += 1;
    session.current = value;
    queue.current = null;
    current.current = null;
    currentStep.current = null;
    refill.current = null;
    prepared.current.clear();
    frozenSubmit.current = null;
    frozenAdvance.current = null;
    setSubmissionConflict(false);
    setSubmissionPending(false);
    setSubmissionError(null);
    setResource(empty);
  }, []);

  const clear = useCallback(() => {
    epoch.current += 1;
    current.current = null;
    currentStep.current = null;
    setResource(empty);
  }, []);
  useEffect(() => () => { epoch.current += 1; }, []);

  const reserve = useCallback((token: string, pin: number | null = null) => {
    refillAgain.current = true;
    if (refill.current) return refill.current;
    const generation = epoch.current;
    const pending = (async () => {
      let latest: QueueSnapshot;
      do {
        refillAgain.current = false;
        latest = await api.questionQueue(token, { action: 'reserve', ...(pin === null ? {} : { pinned_question_id: pin }) });
        if (generation !== epoch.current) throw new Error('Session changed');
        if (queue.current && latest.session_generation !== queue.current.session_generation) throw new Error('Session changed');
        if (!queue.current || latest.revision >= queue.current.revision) queue.current = latest;
      } while (refillAgain.current);
      return queue.current!;
    })();
    refill.current = pending;
    void pending.finally(() => { if (refill.current === pending) refill.current = null; }).catch(() => {});
    return pending;
  }, []);

  const loadAssistance = useCallback(async () => {
    const s = session.current;
    const q = current.current;
    if (!s || !q || !s.assistance_method || s.assistance_method === 'none') return;
    const generation = epoch.current;
    setResource(previous => ({ ...previous, loading: true, error: null }));
    try {
      const step = await api.startAssistance(s.rater_session_token, q.id);
      if (generation !== epoch.current || current.current?.id !== q.id) return;
      // Retain draft subtask answers when reconciliation returns the same step.
      const accepted = JSON.stringify(currentStep.current) === JSON.stringify(step) ? currentStep.current! : step;
      currentStep.current = accepted;
      setResource(previous => ({ ...previous, step: accepted, loading: false, error: null }));
    } catch (error) {
      if (generation === epoch.current) setResource(previous => ({ ...previous, loading: false, error: message(error) }));
    }
  }, []);

  const load = useCallback(async (token: string, pin: number | null) => {
    const generation = ++epoch.current;
    // A refill from the previous visible question may finish later. Demand is
    // retained by reserve's loop; no old callback can publish a visible step.
    if (refill.current) {
      await refill.current.catch(() => {});
      refill.current = null;
    }
    if (generation !== epoch.current) return undefined;
    currentStep.current = null;
    current.current = null;
    frozenAdvance.current = null;
    setSubmissionError(null);
    setSubmissionConflict(false);
    setResource(empty);
    let question: Question | null;
    if (session.current?.queue_enabled) {
      let state = await reserve(token, pin);
      for (let attempt = 0; state.items[0] && !state.items[0].activated; attempt += 1) {
        if (attempt >= 3) throw new Error('The question changed in another tab. Please retry.');
        const head = state.items[0];
        try {
          state = await api.questionQueue(token, { action: 'activate', revision: state.revision, assignment_id: head.assignment_id, generation: head.generation });
          if (generation !== epoch.current) return undefined;
          if (!queue.current || state.revision >= queue.current.revision) queue.current = state;
          state = queue.current;
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 409) throw error;
          state = await reserve(token);
        }
      }
      question = state.items[0]?.question ?? null;
    } else {
      question = pin === null ? await api.getNextQuestion(token) : await api.getQuestion(token, pin);
    }
    if (generation !== epoch.current) return undefined;
    current.current = question;
    setResource({ ...empty, question, loading: Boolean(question && session.current?.assistance_method && session.current.assistance_method !== 'none') });
    void loadAssistance();
    const state = queue.current;
    if (state?.prefetch_enabled && state.phase === 'active') {
      for (const item of state.items.filter(item => !item.activated)) {
        const key = `${item.assignment_id}:${item.generation}`;
        if (!prepared.current.has(key)) {
          prepared.current.add(key);
          void api.prepareAssistance(token, item.assignment_id, item.generation).catch(() => {
            if (generation === epoch.current) prepared.current.delete(key);
          });
        }
      }
    }
    return question;
  }, [reserve, loadAssistance]);

  const advance = useCallback(async (answers: Answers) => {
    const step = currentStep.current;
    const s = session.current;
    if (!step || !s || advancing.current) return;
    advancing.current = true;
    const generation = epoch.current;
    const pending = frozenAdvance.current ?? { sessionId: step.session_id, revision: step.revision ?? 0, answers: JSON.parse(JSON.stringify(answers)) as Answers };
    frozenAdvance.current = pending;
    try {
      const next = await api.advanceAssistance(s.rater_session_token, pending.sessionId, pending.answers, pending.revision);
      if (generation !== epoch.current) return;
      frozenAdvance.current = null;
      currentStep.current = next;
      setResource(previous => ({ ...previous, step: next, error: null }));
    } catch (error) {
      if (generation === epoch.current) {
        if (error instanceof ApiError && error.status < 500 && error.status !== 409) frozenAdvance.current = null;
        setResource(previous => ({ ...previous, error: message(error) }));
      }
    } finally { advancing.current = false; }
  }, []);

  const retryAssistance = useCallback(async () => {
    if (frozenAdvance.current) await advance(frozenAdvance.current.answers);
    else await loadAssistance();
  }, [advance, loadAssistance]);

  const submit = useCallback(async (payload: RatingSubmit) => {
    if (!session.current || submitting.current) throw new Error('Submission already in progress');
    submitting.current = true;
    const generation = epoch.current;
    const head = queue.current?.items.find(item => item.activated);
    const body = frozenSubmit.current ?? { ...payload, ...(head ? { assignment_id: head.assignment_id, assignment_generation: head.generation } : {}) };
    frozenSubmit.current = body;
    setSubmissionError(null);
    setSubmissionPending(true);
    try {
      await api.submitRating(session.current.rater_session_token, body);
      if (generation !== epoch.current) throw new Error('Session changed');
      frozenSubmit.current = null;
      setSubmissionPending(false);
    } catch (error) {
      if (generation !== epoch.current) throw new Error('Session changed');
      setSubmissionConflict(error instanceof ApiError && error.status === 409);
      const uncertain = !(error instanceof ApiError) || error.status >= 500;
      setSubmissionPending(uncertain);
      if (!uncertain) frozenSubmit.current = null;
      setSubmissionError(uncertain ? 'We could not confirm whether your answer was saved. Retry saving the same answer.' : message(error));
      throw error;
    } finally { submitting.current = false; }
  }, []);

  const retrySubmission = useCallback(async () => {
    if (!frozenSubmit.current) throw new Error('No pending submission');
    await submit(frozenSubmit.current);
  }, [submit]);

  const skip = useCallback(async (token: string) => {
    const state = queue.current;
    const head = state?.items[0];
    if (session.current?.queue_enabled && state && head) {
      queue.current = await api.questionQueue(token, { action: 'skip', revision: state.revision, assignment_id: head.assignment_id, generation: head.generation });
    }
  }, []);

  return { question: resource.question, step: resource.step, configure, clear, load, submit, retrySubmission, submissionError, submissionPending, submissionConflict, skip,
    assistance: { step: resource.step, loading: resource.loading, error: resource.error, advance, retry: retryAssistance } satisfies AssistanceResource };
}
