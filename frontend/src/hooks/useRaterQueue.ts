import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import type { AssistanceStep, Question, RatingSubmit, Session } from '../types';

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

// Retry only transport failures. Durable start reattaches to the same job;
// human-input advances retain their separate explicit retry behavior.
const ASSISTANCE_ATTEMPTS = 6;
function retryDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const aborted = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', aborted);
      resolve();
    }, milliseconds);
    if (signal.aborted) aborted();
    else signal.addEventListener('abort', aborted, { once: true });
  });
}

/** Owns the displayed question, assistance loading, and retry identities. */
export function useRaterQueue() {
  const [resource, setResource] = useState(empty);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [submissionConflict, setSubmissionConflict] = useState(false);
  const [submissionPending, setSubmissionPending] = useState(false);
  const session = useRef<Session | null>(null);
  const epoch = useRef(0);
  const assistanceRequest = useRef<AbortController | null>(null);
  const current = useRef<Question | null>(null);
  const currentStep = useRef<AssistanceStep | null>(null);
  const frozenSubmit = useRef<RatingSubmit | null>(null);
  const submitting = useRef(false);
  const advancing = useRef(false);
  const frozenAdvance = useRef<{ sessionId: number; turn: number; answers: Answers } | null>(null);

  const configure = useCallback((value: Session) => {
    epoch.current += 1;
    assistanceRequest.current?.abort();
    session.current = value;
    current.current = null;
    currentStep.current = null;
    frozenSubmit.current = null;
    frozenAdvance.current = null;
    setSubmissionConflict(false);
    setSubmissionPending(false);
    setSubmissionError(null);
    setResource(empty);
  }, []);

  const clear = useCallback(() => {
    epoch.current += 1;
    assistanceRequest.current?.abort();
    current.current = null;
    currentStep.current = null;
    setResource(empty);
  }, []);
  useEffect(() => () => { epoch.current += 1; assistanceRequest.current?.abort(); }, []);

  const loadAssistance = useCallback(async () => {
    const s = session.current;
    const q = current.current;
    if (!s || !q || !s.assistance_method || s.assistance_method === 'none') return;
    const generation = epoch.current;
    const started = performance.now();
    assistanceRequest.current?.abort();
    const controller = new AbortController();
    assistanceRequest.current = controller;
    setResource(previous => ({ ...previous, loading: true, error: null }));
    try {
      for (let attempt = 0; attempt < ASSISTANCE_ATTEMPTS; attempt += 1) {
        try {
          const step = await api.startAssistance(s.rater_session_token, q.id, controller.signal);
          if (controller.signal.aborted || generation !== epoch.current || current.current?.id !== q.id) return;
          // Retain draft subtask answers when reconciliation returns the same step.
          const accepted = JSON.stringify(currentStep.current) === JSON.stringify(step) ? currentStep.current! : step;
          currentStep.current = accepted;
          void api.observeAssistance(s.rater_session_token, step.session_id, performance.now() - started).catch(() => {});
          setResource(previous => ({ ...previous, step: accepted, loading: false, error: null }));
          return;
        } catch (error) {
          if (controller.signal.aborted) return;
          const retryable = error instanceof TypeError || (error instanceof ApiError && [502, 503, 504].includes(error.status));
          if (!retryable || attempt === ASSISTANCE_ATTEMPTS - 1) throw error;
          await retryDelay(Math.min(1000 * 2 ** attempt, 5000), controller.signal);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && generation === epoch.current) setResource(previous => ({ ...previous, loading: false, error: message(error) }));
    } finally {
      if (assistanceRequest.current === controller) assistanceRequest.current = null;
    }
  }, []);

  const load = useCallback(async (token: string, pin: number | null) => {
    const generation = ++epoch.current;
    assistanceRequest.current?.abort();
    currentStep.current = null;
    current.current = null;
    frozenAdvance.current = null;
    setSubmissionError(null);
    setSubmissionConflict(false);
    setResource(empty);
    const question = pin === null ? await api.getNextQuestion(token) : await api.getQuestion(token, pin);
    if (generation !== epoch.current) return undefined;
    current.current = question;
    setResource({ ...empty, question, loading: Boolean(question && session.current?.assistance_method && session.current.assistance_method !== 'none') });
    void loadAssistance();
    return question;
  }, [loadAssistance]);

  const advance = useCallback(async (answers: Answers) => {
    const step = currentStep.current;
    const s = session.current;
    if (!step || !s || advancing.current) return;
    advancing.current = true;
    const generation = epoch.current;
    const pending = frozenAdvance.current ?? { sessionId: step.session_id, turn: step.turn, answers: JSON.parse(JSON.stringify(answers)) as Answers };
    frozenAdvance.current = pending;
    try {
      const next = await api.advanceAssistance(s.rater_session_token, { session_id: pending.sessionId, turn: pending.turn }, pending.answers);
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
    const question = current.current;
    const body = frozenSubmit.current ?? { ...payload, ...(question?.assignment_id != null ? { assignment_id: question.assignment_id, assignment_generation: question.assignment_generation! } : {}) };
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
    const question = current.current;
    if (question?.assignment_id != null && question.assignment_generation != null) {
      await api.skipQuestion(token, question.assignment_id, question.assignment_generation);
    }
  }, []);

  return { question: resource.question, step: resource.step, configure, clear, load, submit, retrySubmission, submissionError, submissionPending, submissionConflict, skip,
    assistance: { step: resource.step, loading: resource.loading, error: resource.error, advance, retry: retryAssistance } satisfies AssistanceResource };
}
