// Real-time WebSocket Control Client for MentorPi Pi 5
import type {
  Challenge,
  MotionDirection,
  WebControlErrorCode,
  WsClientMessage,
  WsServerMessage,
} from '../types/api';

export type ChallengeHandler = (challenge: Challenge) => MotionDirection;
export type ErrorHandler = (code: WebControlErrorCode | string, message: string) => void;
export type DisconnectHandler = (event: CloseEvent | Event) => void;
export type BoundHandler = (epoch: number, operatorId: string) => void;

export class WsControlClient {
  private ws: WebSocket | null = null;
  private sequence: number = 1;
  private isBound: boolean = false;
  private activeEpoch: number | null = null;
  private activeOperatorId: string | null = null;
  private pendingIntent: boolean = false;

  private onChallengeCallback: ChallengeHandler | null = null;
  private onErrorCallback: ErrorHandler | null = null;
  private onDisconnectCallback: DisconnectHandler | null = null;
  private onBoundCallback: BoundHandler | null = null;

  constructor(
    private urlProvider: () => string = () => {
      const loc = window.location;
      const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${proto}//${loc.host}/api/v1/control`;
    }
  ) {}

  setHandlers(handlers: {
    onChallenge?: ChallengeHandler;
    onError?: ErrorHandler;
    onDisconnect?: DisconnectHandler;
    onBound?: BoundHandler;
  }) {
    if (handlers.onChallenge) this.onChallengeCallback = handlers.onChallenge;
    if (handlers.onError) this.onErrorCallback = handlers.onError;
    if (handlers.onDisconnect) this.onDisconnectCallback = handlers.onDisconnect;
    if (handlers.onBound) this.onBoundCallback = handlers.onBound;
  }

  get isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  get isSessionBound(): boolean {
    return this.isBound;
  }

  get hasPendingIntent(): boolean {
    return this.pendingIntent;
  }

  connect(): Promise<void> {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return Promise.resolve();
    }

    return new Promise((resolve, reject) => {
      try {
        const url = this.urlProvider();
        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
          this.pendingIntent = false;
          resolve();
        };

        this.ws.onerror = (err) => {
          if (!this.isBound) {
            reject(err);
          }
        };

        this.ws.onclose = (event) => {
          this.isBound = false;
          this.activeEpoch = null;
          this.activeOperatorId = null;
          this.pendingIntent = false;
          if (this.onDisconnectCallback) {
            this.onDisconnectCallback(event);
          }
        };

        this.ws.onmessage = (event) => {
          this.handleMessage(event.data);
        };
      } catch (err) {
        reject(err);
      }
    });
  }

  bind(operatorId: string, epoch: number, bindToken: string): void {
    if (!this.isConnected) {
      throw new Error('WebSocket is not connected');
    }

    this.activeOperatorId = operatorId;
    this.activeEpoch = epoch;
    this.sequence = 1;

    const bindMsg: WsClientMessage = {
      action: 'bind',
      payload: {
        operator_id: operatorId,
        epoch: epoch,
        bind_token: bindToken,
      },
    };

    this.sendJson(bindMsg);
  }

  updateEpoch(newEpoch: number): void {
    this.activeEpoch = newEpoch;
  }

  sendStop(requestId: string = 'ws-stop', epoch?: number | null): void {
    if (!this.isConnected) return;

    this.pendingIntent = false;
    const stopMsg: WsClientMessage = {
      action: 'stop',
      payload: {
        request_id: requestId,
        epoch: epoch ?? this.activeEpoch,
      },
    };
    this.sendJson(stopMsg);
  }

  disconnect(): void {
    this.isBound = false;
    this.activeEpoch = null;
    this.activeOperatorId = null;
    this.pendingIntent = false;
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // Ignore errors during disconnect
      }
      this.ws = null;
    }
  }

  private sendJson(data: any): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(data));
      } catch (err) {
        console.error('Failed to send WebSocket frame:', err);
      }
    }
  }

  private handleMessage(raw: string): void {
    let msg: WsServerMessage;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    if (msg.type === 'challenge') {
      const challenge: Challenge = msg.payload as Challenge;
      this.handleChallenge(challenge);
    } else if (msg.type === 'ack') {
      const payload = msg.payload || {};
      if (payload.action === 'bind' && payload.success) {
        this.isBound = true;
        if (this.onBoundCallback && this.activeEpoch !== null && this.activeOperatorId !== null) {
          this.onBoundCallback(this.activeEpoch, this.activeOperatorId);
        }
      }
      if (payload.action === 'intent') {
        this.pendingIntent = false;
      }
    } else if (msg.type === 'error') {
      const errCode = msg.payload?.error || 'UNKNOWN_ERROR';
      const errMsg = msg.payload?.message || 'WebSocket error';
      this.pendingIntent = false;
      // Lease expiry revokes motion authority, not the WebSocket's owner binding.
      // Keep answering challenges after an explicit Arm restores authority.
      if (errCode === 'NOT_OWNER') {
        this.isBound = false;
      }
      if (this.onErrorCallback) {
        this.onErrorCallback(errCode, errMsg);
      }
    }
  }

  private handleChallenge(challenge: Challenge): void {
    if (!this.isBound || this.activeEpoch === null) return;

    // Accept challenge if epoch matches or has advanced on the server
    if (challenge.epoch > this.activeEpoch) {
      this.activeEpoch = challenge.epoch;
    } else if (challenge.epoch !== this.activeEpoch) {
      return;
    }

    // Query active direction from callback
    let direction: MotionDirection = 'neutral';
    if (this.onChallengeCallback) {
      direction = this.onChallengeCallback(challenge);
    }

    // Submit intent response
    const intentMsg: WsClientMessage = {
      action: 'intent',
      payload: {
        token: challenge.token,
        epoch: challenge.epoch,
        sequence: this.sequence++,
        direction: direction,
        client_timestamp_ms: Date.now(),
      },
    };

    this.pendingIntent = true;
    this.sendJson(intentMsg);
  }
}

export const wsControlClient = new WsControlClient();
