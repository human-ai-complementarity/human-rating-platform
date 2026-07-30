import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api';
import Timer from './Timer';
import QuestionCard, { parseOptions } from './QuestionCard';
import type { AnswerRequest } from './QuestionCard';
import AssistancePanel from './AssistancePanel';
import RaterIntro from './RaterIntro';
import type { Session, Question, AssistanceStep } from '../types';

const STORAGE_KEY = 'hrp_rater_session';

type SessionPayload = Omit<Session, 'rater_session_token'> & {
  rater_session_token?: string;
};

type StoredSession = {
  experimentId?: string;
  session: Session;
  token: string;
  introAcknowledged: boolean;
};

type ProlificSessionParams = {
  experimentId: string;
  prolificId: string;
  studyId: string;
  sessionId: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isSessionPayload(value: unknown): value is SessionPayload {
  return (
    isRecord(value) &&
    typeof value.rater_id === 'number' &&
    typeof value.session_start === 'string' &&
    typeof value.session_end_time === 'string' &&
    typeof value.experiment_name === 'string' &&
    (value.experiment_description_html === null ||
      value.experiment_description_html === undefined ||
      typeof value.experiment_description_html === 'string') &&
    (value.completion_url === null || typeof value.completion_url === 'string') &&
    (value.rater_session_token === undefined || typeof value.rater_session_token === 'string') &&
    (value.assistance_instructions === null ||
      value.assistance_instructions === undefined ||
      typeof value.assistance_instructions === 'string')
  );
}

function parseStoredSession(raw: string): StoredSession | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed) || !isSessionPayload(parsed.session)) {
      return null;
    }

    const token =
      parsed.session.rater_session_token ??
      (typeof parsed.token === 'string' ? parsed.token : null);
    if (!token) {
      return null;
    }

    const sessionPayload = parsed.session;
    const session: Session = {
      ...sessionPayload,
      rater_session_token: token,
      experiment_description_html: sessionPayload.experiment_description_html ?? null,
      assistance_instructions: sessionPayload.assistance_instructions ?? null,
    };

    return {
      experimentId: typeof parsed.experimentId === 'string' ? parsed.experimentId : undefined,
      session,
      token,
      introAcknowledged: parsed.introAcknowledged === true,
    };
  } catch {
    return null;
  }
}

function canResumeStoredSession(
  storedSession: StoredSession,
  currentExperimentId: string | null
): boolean {
  if (!currentExperimentId) {
    return true;
  }

  return storedSession.experimentId === currentExperimentId;
}

function getProlificSessionParams(params: {
  experimentId: string | null;
  prolificId: string | null;
  studyId: string | null;
  sessionId: string | null;
}): ProlificSessionParams | null {
  const { experimentId, prolificId, studyId, sessionId } = params;
  if (!experimentId || !prolificId || !studyId || !sessionId) {
    return null;
  }

  return {
    experimentId,
    prolificId,
    studyId,
    sessionId,
  };
}

function hasIntroContent(session: Session): boolean {
  return Boolean(session.experiment_description_html || session.assistance_instructions);
}

function RaterView() {
  const [searchParams] = useSearchParams();
  const [session, setSession] = useState<Session | null>(null);
  const [sessionToken, setSessionToken] = useState<string | null>(null);
  const [question, setQuestion] = useState<Question | null>(null);
  const [questionsCompleted, setQuestionsCompleted] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [allDone, setAllDone] = useState(false);
  const [assistanceSessionId, setAssistanceSessionId] = useState<number | null>(null);
  const [assistanceStep, setAssistanceStep] = useState<AssistanceStep | null>(null);
  const [showIntro, setShowIntro] = useState(false);
  // Mirror of the rater's current answer plus a channel for pushing an answer
  // into the question card (used when a Top-N suggestion is clicked).
  const [raterAnswer, setRaterAnswer] = useState('');
  const [answerRequest, setAnswerRequest] = useState<AnswerRequest | null>(null);

  const experimentId = searchParams.get('experiment_id');
  const prolificId = searchParams.get('PROLIFIC_PID');
  const studyId = searchParams.get('STUDY_ID');
  const sessionId = searchParams.get('SESSION_ID');
  const isPreview = searchParams.get('preview') === 'true';
  const prolificSessionParams = useMemo(
    () => getProlificSessionParams({ experimentId, prolificId, studyId, sessionId }),
    [experimentId, prolificId, studyId, sessionId]
  );

  const clearStoredSession = useCallback(() => {
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore storage failures (private mode, quota, etc.)
    }
  }, []);

  const persistSession = useCallback((nextSession: Session, introAcknowledged: boolean) => {
    try {
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ session: nextSession, experimentId, introAcknowledged })
      );
    } catch {
      // Ignore storage failures (private mode, quota, etc.)
    }
  }, [experimentId]);

  const markIntroAcknowledged = useCallback(() => {
    try {
      const stored = sessionStorage.getItem(STORAGE_KEY);
      if (!stored) return;
      const parsed = JSON.parse(stored);
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ ...parsed, introAcknowledged: true })
      );
    } catch {
      // Ignore storage failures (private mode, quota, etc.)
    }
  }, []);

  const fetchNextQuestion = useCallback(async (token: string) => {
    try {
      setAssistanceSessionId(null);
      setAssistanceStep(null);
      setAnswerRequest(null);
      setRaterAnswer('');
      const q = await api.getNextQuestion(token);
      if (q === null || (typeof q === 'object' && Object.keys(q).length === 0)) {
        setAllDone(true);
        setQuestion(null);
      } else {
        setAllDone(false);
        setQuestion(q);
        try {
          const stored = sessionStorage.getItem(STORAGE_KEY);
          if (stored) {
            const parsed = JSON.parse(stored);
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ ...parsed, question: q }));
          }
        } catch { /* ignore */ }
      }
    } catch (err) {
      if (err instanceof Error && err.message === 'Session expired') {
        setSessionExpired(true);
      } else {
        setError(err instanceof Error ? err.message : 'Unknown error');
      }
    }
  }, []);

  const loadNextQuestion = useCallback(async (token: string) => {
    setLoading(true);
    try {
      await fetchNextQuestion(token);
    } finally {
      setLoading(false);
    }
  }, [fetchNextQuestion]);

  const restoreStoredSession = useCallback(async (storedSession: StoredSession) => {
    setSession(storedSession.session);
    setSessionToken(storedSession.token);
    setLoading(true);

    try {
      const [status] = await Promise.all([
        api.getSessionStatus(storedSession.token).catch(() => null),
        fetchNextQuestion(storedSession.token),
      ]);

      if (status) {
        setQuestionsCompleted(status.questions_completed);
      }

      // Re-show intro only if it was never acknowledged AND no questions
      // completed yet — otherwise a refresh mid-rating shouldn't replay it.
      const completed = status?.questions_completed ?? 0;
      if (
        !storedSession.introAcknowledged &&
        completed === 0 &&
        hasIntroContent(storedSession.session)
      ) {
        setShowIntro(true);
      }
    } finally {
      setLoading(false);
    }
  }, [fetchNextQuestion]);

  const startRaterSession = useCallback(async (params: ProlificSessionParams) => {
    try {
      const nextSession = await api.startSession(
        params.experimentId,
        params.prolificId,
        params.studyId,
        params.sessionId,
        isPreview
      );
      setSession(nextSession);
      setSessionToken(nextSession.rater_session_token);
      const needsIntro = hasIntroContent(nextSession);
      persistSession(nextSession, !needsIntro);
      if (needsIntro) {
        setShowIntro(true);
        setLoading(false);
      } else {
        await loadNextQuestion(nextSession.rater_session_token);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      setLoading(false);
    }
  }, [isPreview, persistSession, loadNextQuestion]);

  const handleIntroContinue = useCallback(() => {
    if (!sessionToken) return;
    markIntroAcknowledged();
    setShowIntro(false);
    // On fresh start we skipped the question fetch; on restore it's already
    // loaded. Only fetch when we don't yet have one.
    if (!question) {
      void loadNextQuestion(sessionToken);
    }
  }, [sessionToken, markIntroAcknowledged, loadNextQuestion, question]);

  const startedRef = useRef(false);

  useEffect(() => {
    if (startedRef.current) {
      return;
    }

    const rawStoredSession = sessionStorage.getItem(STORAGE_KEY);
    if (rawStoredSession) {
      const storedSession = parseStoredSession(rawStoredSession);
      if (!storedSession) {
        clearStoredSession();
      } else if (canResumeStoredSession(storedSession, experimentId)) {
        startedRef.current = true;
        void restoreStoredSession(storedSession);
        return;
      }
    }

    if (!prolificSessionParams) {
      setError('Please access this study from Prolific.');
      setLoading(false);
      return;
    }

    // Prevent React StrictMode double-mount from firing two concurrent
    // startSession requests, which causes a unique constraint violation.
    startedRef.current = true;
    void startRaterSession(prolificSessionParams);
  }, [
    experimentId,
    prolificSessionParams,
    startRaterSession,
    restoreStoredSession,
    clearStoredSession,
  ]);

  useEffect(() => {
    if (!(sessionExpired || allDone)) return;
    const completionUrl = session?.completion_url;

    // Clear persisted session once we're done or expired (always)
    clearStoredSession();

    if (!completionUrl) return;

    const timer = setTimeout(() => {
      window.location.href = completionUrl;
    }, 3000);
    return () => clearTimeout(timer);
  }, [sessionExpired, allDone, session?.completion_url, clearStoredSession]);

  const handleSubmit = async (answer: string, confidence: number, timeStarted: string) => {
    if (!session || !question || !sessionToken) return;

    try {
      await api.submitRating(sessionToken, {
        question_id: question.id,
        answer,
        confidence,
        time_started: timeStarted,
        ...(assistanceSessionId !== null ? { assistance_session_id: assistanceSessionId } : {}),
      });
      setQuestionsCompleted(prev => prev + 1);
      await loadNextQuestion(sessionToken);
    } catch (err) {
      if (err instanceof Error && err.message === 'Session expired') {
        setSessionExpired(true);
      } else {
        setError(err instanceof Error ? err.message : 'Unknown error');
      }
    }
  };

const handleSessionExpired = () => {
    setSessionExpired(true);
  };

  // Auto-skip question when decomposition fails mid-session — question stays unrated and may reappear
  useEffect(() => {
    if (assistanceStep?.type === 'skip' && sessionToken) {
      void loadNextQuestion(sessionToken);
    }
  }, [assistanceStep?.type, sessionToken, loadNextQuestion]);

  const questionOptions = useMemo(
    () => (question ? parseOptions(question.options) : []),
    [question]
  );

  const handleSelectSuggestion = useCallback((answer: string) => {
    setAnswerRequest(prev => ({ answer, seq: (prev?.seq ?? 0) + 1 }));
  }, []);

  const hasAssistance = session?.assistance_method && session.assistance_method !== 'none';
  const assistanceBlocksRating = Boolean(
    hasAssistance &&
      question !== null &&
      (assistanceStep === null || assistanceStep.type === 'ask_input')
  );
  // Collapse to single column when assistance returned 'none' (LLM decided no help needed)

  const styles = {
    container: {
      maxWidth: hasAssistance ? '1200px' : '720px',
      margin: '0 auto',
      padding: '32px 24px',
      minHeight: '100vh',
    },
    header: {
      background: 'var(--surface)',
      borderRadius: 'var(--radius)',
      border: '1px solid var(--faint)',
      boxShadow: 'var(--shadow)',
      padding: '18px 24px',
      marginBottom: 20,
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
    },
    experimentName: {
      fontFamily: 'var(--font-head)',
      fontSize: 20,
      fontWeight: 600,
      color: 'var(--ink)',
      letterSpacing: '-0.005em',
      margin: 0,
    },
    progress: {
      fontSize: 13,
      color: 'var(--muted)',
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      fontFamily: 'var(--font-mono)',
      letterSpacing: '0.06em',
      textTransform: 'uppercase' as const,
    },
    progressCount: {
      background: 'var(--accent-soft)',
      color: 'var(--accent-soft-ink)',
      padding: '4px 12px',
      borderRadius: 999,
      fontWeight: 700,
      fontFamily: 'var(--font-mono)',
      letterSpacing: '0.03em',
      fontSize: 13,
    },
    completionCard: {
      background: 'var(--surface)',
      borderRadius: 'var(--radius)',
      border: '1px solid var(--faint)',
      boxShadow: 'var(--shadow)',
      padding: '64px 40px',
      textAlign: 'center' as const,
    },
    completionTitle: {
      fontFamily: 'var(--font-head)',
      fontSize: 34,
      fontWeight: 600,
      color: 'var(--accent-soft-ink)',
      letterSpacing: '-0.01em',
      marginBottom: 14,
    },
    completionText: {
      fontSize: 16,
      color: 'var(--muted)',
      marginBottom: 26,
      lineHeight: 1.55,
    },
    completionStats: {
      fontSize: 15,
      color: 'var(--ink)',
      marginBottom: 32,
    },
    completionCount: {
      fontFamily: 'var(--font-head)',
      fontSize: 52,
      fontWeight: 600,
      color: 'var(--accent)',
      display: 'block',
      marginBottom: 6,
      letterSpacing: '-0.02em',
    },
    redirectText: {
      fontSize: 13,
      color: 'var(--muted)',
      lineHeight: 1.6,
    },
    redirectLink: {
      color: 'var(--accent-soft-ink)',
      textDecoration: 'underline',
      fontWeight: 600,
    },
    errorCard: {
      background: 'var(--surface)',
      borderRadius: 'var(--radius)',
      border: '1px solid var(--danger-soft)',
      boxShadow: 'var(--shadow)',
      padding: 40,
      textAlign: 'center' as const,
    },
    errorText: {
      color: 'var(--danger)',
      fontSize: 15,
      lineHeight: 1.55,
    },
    loadingCard: {
      background: 'var(--surface)',
      borderRadius: 'var(--radius)',
      border: '1px solid var(--faint)',
      boxShadow: 'var(--shadow)',
      padding: '64px 40px',
      textAlign: 'center' as const,
      color: 'var(--muted)',
      fontSize: 15,
    },
  };

  const previewBannerStyle = {
    background: 'var(--warn-soft)',
    border: '1px solid var(--warn)',
    borderRadius: 'var(--radius-sm)',
    padding: '11px 16px',
    marginBottom: 16,
    fontSize: 13,
    color: 'var(--warn)',
    lineHeight: 1.5,
  };

  if (error) {
    return (
      <div style={styles.container}>
        <div style={styles.errorCard}>
          <p style={styles.errorText}>{error}</p>
        </div>
      </div>
    );
  }

  if (sessionExpired || allDone) {
    const completionUrl = session?.completion_url;

    return (
      <div style={styles.container}>
        <div style={styles.completionCard}>
          <h1 style={styles.completionTitle}>
            {allDone ? 'All Done!' : 'Session Complete'}
          </h1>
          <p style={styles.completionText}>
            {allDone
              ? 'You have completed all available questions.'
              : 'Your session has ended.'}
          </p>
          <div style={styles.completionStats}>
            <span style={styles.completionCount}>{questionsCompleted}</span>
            questions completed
          </div>
          {completionUrl ? (
            <p style={styles.redirectText}>
              Redirecting you back to Prolific in 3 seconds...
              <br />
              <a href={completionUrl} style={styles.redirectLink}>
                Click here if not redirected
              </a>
            </p>
          ) : (
            <p style={styles.redirectText}>
              Thank you for your participation! You may now close this window.
            </p>
          )}
        </div>
      </div>
    );
  }

  if (loading || !session) {
    return (
      <div style={styles.container}>
        <div style={styles.loadingCard}>
          Loading...
        </div>
      </div>
    );
  }

  if (showIntro) {
    return (
      <div style={{ ...styles.container, maxWidth: '720px' }}>
        {isPreview && (
          <div style={previewBannerStyle}>
            Preview mode — ratings submitted here are real and will appear in your data.
          </div>
        )}
        <RaterIntro
          experimentName={session.experiment_name}
          descriptionHtml={session.experiment_description_html}
          assistanceInstructions={session.assistance_instructions}
          onContinue={handleIntroContinue}
        />
      </div>
    );
  }

  return (
    <div style={styles.container}>
      {isPreview && (
        <div style={previewBannerStyle}>
          Preview mode — ratings submitted here are real and will appear in your data.
        </div>
      )}
      <Timer sessionEndTime={session.session_end_time} onExpire={handleSessionExpired} />

      <div style={styles.header}>
        <h2 style={styles.experimentName}>{session.experiment_name}</h2>
        <div style={styles.progress}>
          Completed
          <span style={styles.progressCount}>{questionsCompleted}</span>
        </div>
      </div>

      {hasAssistance && question && sessionToken ? (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px', alignItems: 'start' }}>
          <div>
            <QuestionCard
              question={question}
              onSubmit={handleSubmit}
              assistanceActive={assistanceBlocksRating}
              assistanceAnswer={assistanceStep?.payload.synthesis?.answer ?? null}
              answerRequest={answerRequest}
              onAnswerChange={setRaterAnswer}
              humanPromptPrefix={session?.human_prompt_prefix ?? null}
              humanPromptSuffix={session?.human_prompt_suffix ?? null}
            />
          </div>
          <AssistancePanel
            sessionToken={sessionToken}
            questionId={question.id}
            onSessionId={setAssistanceSessionId}
            onStepChange={setAssistanceStep}
            questionOptions={questionOptions}
            selectedAnswer={raterAnswer}
            onSelectAnswer={handleSelectSuggestion}
          />
        </div>
      ) : (
        <>
          {question && (
            <QuestionCard
              question={question}
              onSubmit={handleSubmit}
              humanPromptPrefix={session?.human_prompt_prefix ?? null}
              humanPromptSuffix={session?.human_prompt_suffix ?? null}
            />
          )}
        </>
      )}
    </div>
  );
}

export default RaterView;
