'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

export type VoiceStatus = 'off' | 'listening' | 'thinking' | 'speaking';

// Silence after a finalised phrase before the caller's turn is sent. A fixed
// short wait cuts people off while they think; a long one makes every answer
// feel slow. So the wait depends on how finished the words sound.
const END_OF_TURN_SHORT_MS = 700; // "yes", "no", "that's right"
const END_OF_TURN_MS = 1400;
const END_OF_TURN_UNFINISHED_MS = 2600; // "...because", "...um", "...and my"
const SHORT_ANSWER = /^(yes|yeah|yep|yup|no|nope|nah|correct|right|that's right|okay|ok|sure|sounds good|perfect)\b[\w\s']{0,12}$/i;
const UNFINISHED = /\b(and|but|so|or|because|cause|like|um+|uh+|er+|hmm+|the|a|an|my|to|of|for|with|about|is|was|i|i'm|it's|that|which|then|since|if|when|just|maybe|it|been)\s*$/i;
// Speaker mode: keep the mic off this long after the agent stops talking, so
// the tail of its own voice isn't picked up.
const SPEAKER_TAIL_MS = 350;

function endOfTurnDelay(text: string): number {
  const trimmed = text.trim();
  if (SHORT_ANSWER.test(trimmed)) return END_OF_TURN_SHORT_MS;
  if (UNFINISHED.test(trimmed)) return END_OF_TURN_UNFINISHED_MS;
  return END_OF_TURN_MS;
}
// If the reply hasn't arrived by now, say something so the line isn't dead.
const FILLER_AFTER_MS = 1800;
const FILLERS = ['One moment.', 'Okay, one second.', 'Let me check that.'];
// Interim speech needs this many words to count as the caller barging in.
const BARGE_IN_MIN_WORDS = 2;

export interface TurnResult {
  reply: string | null;
  /** The call is over (handed to a person): stop listening after the reply. */
  endCall?: boolean;
}

interface UseVoiceCallOptions {
  /** Send one caller turn; resolve with the reply to speak (or null). */
  onTurn: (text: string) => Promise<TurnResult | null>;
  /**
   * true: earphones, mic stays live so the caller can interrupt.
   * false: speaker mode, mic is off while the agent talks so it can't hear
   * itself, at the cost of interruptions.
   */
  bargeIn: boolean;
}

const words = (text: string) => text.toLowerCase().match(/[a-z0-9']+/g) || [];

export function useVoiceCall({ onTurn, bargeIn }: UseVoiceCallOptions) {
  const [supported, setSupported] = useState(true);
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState<VoiceStatus>('off');
  const [interim, setInterim] = useState('');

  const activeRef = useRef(false);
  const thinkingRef = useRef(false);
  const recognitionRef = useRef<any>(null);
  const bufferRef = useRef('');
  const endTimerRef = useRef<number | null>(null);
  const spokenRef = useRef('');
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const voiceRef = useRef<SpeechSynthesisVoice | null>(null);
  const onTurnRef = useRef(onTurn);
  onTurnRef.current = onTurn;
  const bargeInRef = useRef(bargeIn);
  bargeInRef.current = bargeIn;
  // Speaker mode: true while the mic is deliberately off for agent speech.
  const micPausedRef = useRef(false);
  const resumeTimerRef = useRef<number | null>(null);
  const stopRef = useRef<() => void>(() => {});

  useEffect(() => {
    const w = window as any;
    if (!(w.SpeechRecognition || w.webkitSpeechRecognition) || !window.speechSynthesis) {
      setSupported(false);
      return;
    }
    const pickVoice = () => {
      const voices = window.speechSynthesis.getVoices();
      voiceRef.current =
        voices.find((v) => /Google US English|Samantha|Aria|Jenny/i.test(v.name)) ||
        voices.find((v) => v.lang === 'en-US') ||
        null;
    };
    pickVoice();
    window.speechSynthesis.onvoiceschanged = pickVoice;
  }, []);

  // Speaker mode: drop the recognizer while the agent talks (anything heard
  // then is the agent itself) and start a fresh one shortly after it stops.
  // Restarting the same recognizer right after aborting it is racy on desktop
  // Chrome and can leave it dead without an error, so it is never reused.
  const pauseMic = () => {
    if (resumeTimerRef.current) {
      window.clearTimeout(resumeTimerRef.current);
      resumeTimerRef.current = null;
    }
    if (micPausedRef.current) return;
    micPausedRef.current = true;
    const recognizer = recognitionRef.current;
    recognitionRef.current = null;
    if (recognizer) {
      recognizer.onend = null; // a late onend must not restart it
      recognizer.onresult = null;
      try {
        recognizer.abort();
      } catch {
        /* not running */
      }
    }
  };

  const resumeMicSoon = () => {
    if (resumeTimerRef.current) window.clearTimeout(resumeTimerRef.current);
    resumeTimerRef.current = window.setTimeout(() => {
      resumeTimerRef.current = null;
      if (window.speechSynthesis.speaking) return; // the next utterance resumes it
      micPausedRef.current = false;
      if (activeRef.current) startRecognitionRef.current();
    }, SPEAKER_TAIL_MS);
  };
  const startRecognitionRef = useRef<() => void>(() => {});

  const idleStatus = useCallback((): VoiceStatus => {
    if (!activeRef.current) return 'off';
    return thinkingRef.current ? 'thinking' : 'listening';
  }, []);

  const speak = useCallback(
    (text: string) =>
      new Promise<void>((resolve) => {
        if (!text || !window.speechSynthesis) return resolve();
        const utterance = new SpeechSynthesisUtterance(text);
        if (voiceRef.current) utterance.voice = voiceRef.current;
        utterance.rate = 1.05;
        utterance.onstart = () => {
          spokenRef.current = text;
          setStatus('speaking');
          if (!bargeInRef.current) pauseMic();
        };
        const done = () => {
          spokenRef.current = '';
          if (!window.speechSynthesis.speaking) {
            setStatus(idleStatus());
            if (micPausedRef.current) resumeMicSoon();
          }
          resolve();
        };
        utterance.onend = done;
        utterance.onerror = done; // also fires when barge-in cancels speech
        window.speechSynthesis.speak(utterance);
      }),
    [idleStatus],
  );

  // The mic hears the speakers. Drop recognised text that is mostly words the
  // agent is saying right now, so the agent doesn't interrupt itself.
  const isEcho = useCallback((heard: string) => {
    const spoken = spokenRef.current.toLowerCase();
    const heardWords = words(heard);
    if (!spoken || heardWords.length === 0) return false;
    const overlap = heardWords.filter((w) => spoken.includes(w)).length;
    return overlap / heardWords.length >= 0.7;
  }, []);

  const flushTurn = useCallback(() => {
    endTimerRef.current = null;
    const text = bufferRef.current.trim();
    bufferRef.current = '';
    setInterim('');
    if (!text) return;

    queueRef.current = queueRef.current.then(async () => {
      thinkingRef.current = true;
      setStatus('thinking');
      const filler = window.setTimeout(() => {
        void speak(FILLERS[Math.floor(Math.random() * FILLERS.length)]);
      }, FILLER_AFTER_MS);
      try {
        const result = await onTurnRef.current(text);
        window.clearTimeout(filler);
        thinkingRef.current = false;
        if (result?.reply && activeRef.current) await speak(result.reply);
        if (result?.endCall) stopRef.current();
      } catch (err) {
        console.error('voice turn failed', err);
      } finally {
        window.clearTimeout(filler);
        thinkingRef.current = false;
        if (!window.speechSynthesis.speaking) setStatus(idleStatus());
      }
    });
  }, [idleStatus, speak]);

  const flushRef = useRef(flushTurn);
  flushRef.current = flushTurn;

  const startRecognition = useCallback(() => {
    const w = window as any;
    const Recognition = w.SpeechRecognition || w.webkitSpeechRecognition;
    const recognition = new Recognition();
    // Chrome on Android repeats earlier words in continuous mode; take one
    // phrase per session there and let onend restart it.
    recognition.continuous = !/Android/i.test(navigator.userAgent);
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    recognition.onresult = (event: any) => {
      if (micPausedRef.current) return; // speaker mode: agent is talking
      let pending = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const transcript: string = result[0].transcript;
        if (result.isFinal) {
          if (!isEcho(transcript)) bufferRef.current = `${bufferRef.current} ${transcript}`.trim();
        } else {
          pending += transcript;
        }
      }

      const live = isEcho(pending) ? '' : pending.trim();
      const callerTalking = words(live).length >= BARGE_IN_MIN_WORDS;
      if (callerTalking) {
        if (window.speechSynthesis.speaking) window.speechSynthesis.cancel(); // barge-in
        if (endTimerRef.current) {
          window.clearTimeout(endTimerRef.current);
          endTimerRef.current = null;
        }
      }
      setInterim(`${bufferRef.current} ${live}`.trim());

      if (bufferRef.current && !live) {
        if (endTimerRef.current) window.clearTimeout(endTimerRef.current);
        endTimerRef.current = window.setTimeout(() => flushRef.current(), endOfTurnDelay(bufferRef.current));
      }
    };

    // Chrome ends recognition after a stretch of silence; keep it alive
    // (unless speaker mode switched it off on purpose).
    recognition.onend = () => {
      if (!activeRef.current || micPausedRef.current || recognitionRef.current !== recognition) return;
      try {
        recognition.start();
      } catch {
        /* already started */
      }
    };
    recognition.onerror = (event: any) => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        activeRef.current = false;
        setActive(false);
        setStatus('off');
        setSupported(false);
      }
    };

    recognitionRef.current = recognition;
    try {
      recognition.start();
    } catch (err) {
      console.warn('speech recognition failed to start', err);
    }
  }, [isEcho]);
  startRecognitionRef.current = startRecognition;

  const start = useCallback(() => {
    if (!supported || activeRef.current) return;
    // Mobile browsers only allow speech that starts from a tap. Speak a silent
    // utterance inside this click so later replies are allowed to play. Phones
    // only: on desktop Chrome a blank utterance can stall the speech queue.
    if (/Android|iPhone|iPad/i.test(navigator.userAgent)) {
      const unlock = new SpeechSynthesisUtterance(' ');
      unlock.volume = 0;
      window.speechSynthesis.speak(unlock);
    }
    activeRef.current = true;
    setActive(true);
    setStatus('listening');
    startRecognition();
  }, [startRecognition, supported]);

  const stop = useCallback(() => {
    activeRef.current = false;
    setActive(false);
    setStatus('off');
    setInterim('');
    bufferRef.current = '';
    micPausedRef.current = false;
    if (endTimerRef.current) window.clearTimeout(endTimerRef.current);
    if (resumeTimerRef.current) window.clearTimeout(resumeTimerRef.current);
    const recognizer = recognitionRef.current;
    recognitionRef.current = null;
    try {
      recognizer?.stop();
    } catch {
      /* not running */
    }
    window.speechSynthesis?.cancel();
  }, []);

  stopRef.current = stop;
  useEffect(() => stop, [stop]);

  // Caller cuts the agent off (Space or a tap). Cancelling speech fires the
  // utterance's error handler, which brings the mic back after a short tail.
  const interrupt = useCallback(() => {
    if (window.speechSynthesis?.speaking) window.speechSynthesis.cancel();
  }, []);

  return { supported, active, status, interim, start, stop, speak, interrupt };
}
