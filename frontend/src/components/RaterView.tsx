import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api';
import Timer from './Timer';
import QuestionCard from './QuestionCard';
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
    (value.experiment_description === null ||
      value.experiment_description === undefined ||
      typeof value.experiment_description === 'string') &&
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
      experiment_description: sessionPayload.experiment_description ?? null,
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
  return Boolean(session.experiment_description || session.assistance_instructions);
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

  const hasAssistance = session?.assistance_method && session.assistance_method !== 'none';
  // Collapse to single column when assistance returned 'none' (LLM decided no help needed)

  const styles = {
    container: {
      maxWidth: hasAssistance ? '1200px' : '700px',
      margin: '0 auto',
      padding: '24px',
      minHeight: '100vh',
    },
    header: {
      background: '#fff',
      borderRadius: '12px',
      border: '1px solid #e0e0e0',
      padding: '20px 24px',
      marginBottom: '20px',
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
    },
    experimentName: {
      fontSize: '18px',
      fontWeight: 600,
      color: '#333',
      margin: 0,
    },
    progress: {
      fontSize: '14px',
      color: '#666',
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
    },
    progressCount: {
      background: '#e3f2fd',
      color: '#4a90d9',
      padding: '4px 12px',
      borderRadius: '16px',
      fontWeight: 600,
    },
    completionCard: {
      background: '#fff',
      borderRadius: '12px',
      border: '1px solid #e0e0e0',
      padding: '60px 40px',
      textAlign: 'center' as const,
    },
    completionTitle: {
      fontSize: '32px',
      fontWeight: 600,
      color: '#27ae60',
      marginBottom: '16px',
    },
    completionText: {
      fontSize: '16px',
      color: '#666',
      marginBottom: '24px',
    },
    completionStats: {
      fontSize: '18px',
      color: '#333',
      marginBottom: '32px',
    },
    completionCount: {
      fontSize: '48px',
      fontWeight: 700,
      color: '#4a90d9',
      display: 'block',
      marginBottom: '8px',
    },
    redirectText: {
      fontSize: '14px',
      color: '#888',
    },
    redirectLink: {
      color: '#4a90d9',
      textDecoration: 'none',
    },
    errorCard: {
      background: '#fff',
      borderRadius: '12px',
      border: '1px solid #f5c6cb',
      padding: '40px',
      textAlign: 'center' as const,
    },
    errorText: {
      color: '#dc3545',
      fontSize: '16px',
    },
    loadingCard: {
      background: '#fff',
      borderRadius: '12px',
      border: '1px solid #e0e0e0',
      padding: '60px 40px',
      textAlign: 'center' as const,
      color: '#666',
    },
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
      <div style={{ ...styles.container, maxWidth: '700px' }}>
        {isPreview && (
          <div style={{
            background: '#fff3cd',
            border: '1px solid #ffc107',
            borderRadius: '8px',
            padding: '12px 16px',
            marginBottom: '16px',
            fontSize: '14px',
            color: '#856404',
          }}>
            Preview mode — ratings submitted here are real and will appear in your data.
          </div>
        )}
        <RaterIntro
          experimentName={session.experiment_name}
          description={session.experiment_description}
          assistanceInstructions={session.assistance_instructions}
          onContinue={handleIntroContinue}
        />
      </div>
    );
  }

  return (
    <div style={styles.container}>
      {isPreview && (
        <div style={{
          background: '#fff3cd',
          border: '1px solid #ffc107',
          borderRadius: '8px',
          padding: '12px 16px',
          marginBottom: '16px',
          fontSize: '14px',
          color: '#856404',
        }}>
          Preview mode — ratings submitted here are real and will appear in your data.
        </div>
      )}
      <Timer sessionEndTime={session.session_end_time} onExpire={handleSessionExpired} />

      <div style={styles.header}>
        <h2 style={styles.experimentName}>{session.experiment_name}</h2>
        <div style={styles.progress}>
          Completed:
          <span style={styles.progressCount}>{questionsCompleted}</span>
        </div>
      </div>

      {hasAssistance && question && sessionToken ? (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px', alignItems: 'start' }}>
          <div>
            <QuestionCard
              question={question}
              onSubmit={handleSubmit}
              assistanceActive={assistanceStep?.type !== 'complete' && assistanceStep?.type !== 'none'}
              assistanceAnswer={assistanceStep?.payload.synthesis?.answer ?? null}
            />
          </div>
          <AssistancePanel
            sessionToken={sessionToken}
            questionId={question.id}
            onSessionId={setAssistanceSessionId}
            onStepChange={setAssistanceStep}
          />
        </div>
      ) : (
        <>
          {question && (
            <QuestionCard
              question={question}
              onSubmit={handleSubmit}
            />
          )}
        </>
      )}
    </div>
  );
}

export default RaterView;
