'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

export type VoiceStatus = 'off' | 'listening' | 'thinking' | 'speaking';

// Silence after a finalised phrase before the caller's turn is sent. Long
// enough to survive a mid-sentence pause, short enough to feel responsive.
const END_OF_TURN_MS = 800;
// If the reply hasn't arrived by now, say something so the line isn't dead.
const FILLER_AFTER_MS = 1800;
const FILLERS = ['One moment.', 'Okay, one second.', 'Let me check that.'];
// Interim speech needs this many words to count as the caller barging in.
const BARGE_IN_MIN_WORDS = 2;

interface UseVoiceCallOptions {
  /** Send one caller turn; resolve with the reply to speak (or null). */
  onTurn: (text: string) => Promise<string | null>;
}

const words = (text: string) => text.toLowerCase().match(/[a-z0-9']+/g) || [];

export function useVoiceCall({ onTurn }: UseVoiceCallOptions) {
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
        };
        const done = () => {
          spokenRef.current = '';
          if (!window.speechSynthesis.speaking) setStatus(idleStatus());
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
        const reply = await onTurnRef.current(text);
        window.clearTimeout(filler);
        thinkingRef.current = false;
        if (reply && activeRef.current) await speak(reply);
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
        endTimerRef.current = window.setTimeout(() => flushRef.current(), END_OF_TURN_MS);
      }
    };

    // Chrome ends recognition after a stretch of silence; keep it alive.
    recognition.onend = () => {
      if (!activeRef.current) return;
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

    recognition.start();
    recognitionRef.current = recognition;
  }, [isEcho]);

  const start = useCallback(() => {
    if (!supported || activeRef.current) return;
    // Mobile browsers only allow speech that starts from a tap. Speak a silent
    // utterance inside this click so later replies are allowed to play.
    const unlock = new SpeechSynthesisUtterance(' ');
    unlock.volume = 0;
    window.speechSynthesis.speak(unlock);
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
    if (endTimerRef.current) window.clearTimeout(endTimerRef.current);
    try {
      recognitionRef.current?.stop();
    } catch {
      /* not running */
    }
    window.speechSynthesis?.cancel();
  }, []);

  useEffect(() => stop, [stop]);

  return { supported, active, status, interim, start, stop, speak };
}
