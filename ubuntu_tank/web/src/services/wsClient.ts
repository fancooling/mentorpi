// Bound control transport: one outstanding intent, generation fencing, bounded setup.
import type { Challenge, MotionDirection, WsServerMessage } from '../types/api';
export class WsControlClient {
    private ws: WebSocket | null = null;
    private bound = false;
    private epoch: number | null = null;
    private sequence = 0;
    private generation = -1;
    private pending: {
        sentAt: number;
        sequence: number;
        challenge: Challenge;
        direction: MotionDirection;
    } | null = null;
    private rejectSetup: ((error: Error) => void) | null = null;
    private resolveBind: (() => void) | null = null;
    private handlers: {
        onChallenge?: (challenge: Challenge) => MotionDirection | null;
        onAck?: (payload: any, challenge: Challenge, direction: MotionDirection) => void;
        onError?: (code: string, message: string, payload: any) => void;
        onDisconnect?: (event: Event) => void;
    } = {};
    constructor(private urlProvider = () => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/control`) { }
    /** Register consumer callbacks without removing other consumers' handlers. */
    setHandlers(handlers: typeof this.handlers) {
        const prior = this.handlers.onDisconnect;
        const next = handlers.onDisconnect;
        Object.assign(this.handlers, handlers);
        if (prior && next)
            this.handlers.onDisconnect = event => { prior(event); next(event); };
    }
    get isConnected() { return this.ws?.readyState === WebSocket.OPEN; }
    get isSessionBound() { return this.bound; }
    get hasPendingIntent() { return this.pending !== null; }
    /** Open a new socket; superseded socket callbacks cannot affect its state. */
    connect(): Promise<void> {
        this.disconnect();
        const socket = new WebSocket(this.urlProvider());
        this.ws = socket;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => { reject(new Error('Control connection timed out')); this.disconnect(); }, 3000);
            this.rejectSetup = error => { clearTimeout(timer); reject(error); };
            socket.onopen = () => {
                if (this.ws !== socket)
                    return;
                clearTimeout(timer);
                this.rejectSetup = null;
                resolve();
            };
            socket.onerror = () => { if (this.ws === socket)
                this.rejectSetup?.(new Error('Control connection failed')); };
            socket.onclose = event => {
                if (this.ws !== socket)
                    return;
                this.rejectSetup?.(new Error('Control connection closed'));
                this.ws = null;
                this.bound = false;
                this.pending = null;
                this.handlers.onDisconnect?.(event);
            };
            socket.onmessage = event => { if (this.ws === socket)
                this.handleMessage(event.data); };
        });
    }
    /** Resolve only after the runtime acknowledges the single-use binding. */
    bind(operatorId: string, epoch: number, token: string): Promise<void> {
        this.epoch = epoch;
        this.sequence = 0;
        this.generation = -1;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => { this.rejectSetup?.(new Error('Control binding timed out')); }, 3000);
            this.rejectSetup = error => { clearTimeout(timer); this.resolveBind = null; this.rejectSetup = null; reject(error); };
            this.resolveBind = () => { clearTimeout(timer); this.rejectSetup = null; this.resolveBind = null; resolve(); };
            if (!this.isConnected) {
                this.rejectSetup(new Error('Control socket is closed'));
                return;
            }
            this.send({ action: 'bind', payload: { operator_id: operatorId, epoch, bind_token: token } });
        });
    }
    updateEpoch(epoch: number) {
        if (this.epoch !== epoch) {
            this.pending = null;
            this.generation = -1;
        }
        this.epoch = epoch;
    }
    sendStop(requestId = 'ws-stop', epoch?: number | null) {
        this.pending = null;
        this.send({ action: 'stop', payload: { request_id: requestId, epoch: epoch ?? this.epoch } });
    }
    disconnect() {
        this.rejectSetup?.(new Error('Control setup cancelled'));
        const socket = this.ws;
        this.ws = null;
        this.bound = false;
        this.pending = null;
        this.epoch = null;
        this.generation = -1;
        socket?.close();
    }
    private send(message: unknown) {
        if (this.isConnected)
            this.ws!.send(JSON.stringify(message));
    }
    private handleMessage(raw: string) {
        let message: WsServerMessage;
        try {
            message = JSON.parse(raw);
        }
        catch {
            return;
        }
        const payload = message.payload;
        if (message.type === 'ack' && payload.action === 'bind' && payload.success) {
            this.bound = true;
            this.resolveBind?.();
            return;
        }
        if (message.type === 'error') {
            this.rejectSetup?.(new Error(payload.message || 'Control binding rejected'));
            if (payload.sequence === this.pending?.sequence)
                this.pending = null;
            this.handlers.onError?.(payload.error, payload.message, payload);
            return;
        }
        if (message.type === 'ack' && payload.action === 'intent') {
            const sent = this.pending;
            if (!sent || sent.sequence !== payload.sequence || sent.challenge.epoch !== this.epoch || payload.input_generation < this.generation)
                return;
            this.pending = null;
            this.generation = payload.input_generation;
            this.handlers.onAck?.(payload, sent.challenge, sent.direction);
            return;
        }
        if (message.type !== 'challenge' || !this.bound || this.epoch === null)
            return;
        const challenge = payload as Challenge;
        if (challenge.epoch < this.epoch)
            return;
        if (challenge.epoch > this.epoch)
            this.updateEpoch(challenge.epoch);
        if (challenge.input_generation < this.generation)
            return;
        if (challenge.input_generation > this.generation) {
            this.generation = challenge.input_generation;
            this.pending = null;
        }
        // Observe every challenge so a pause is visible even with an outstanding reply.
        const direction = this.handlers.onChallenge?.(challenge) ?? null;
        // A lost neutral response while paused cannot advance the server generation.
        // Retry only fresh neutral challenges; never replay a buffered motion intent.
        if (this.pending && challenge.recovery_required && direction === 'neutral' &&
            performance.now() - this.pending.sentAt >= 500)
            this.pending = null;
        if (this.pending || direction === null)
            return;
        const sequence = ++this.sequence;
        this.pending = { sentAt: performance.now(), sequence, challenge, direction };
        this.send({ action: 'intent', payload: {
                token: challenge.token, epoch: challenge.epoch, input_generation: challenge.input_generation,
                sequence, direction, client_timestamp_ms: Date.now(),
            } });
    }
}
export const wsControlClient = new WsControlClient();
