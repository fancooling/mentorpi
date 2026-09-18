// Typed REST API Client for MentorPi Pi 5 Web Control
import type {
  ControlAcquireRequest,
  ControlAcquireResponse,
  ControlArmRequest,
  ControlArmResponse,
  ControlReleaseRequest,
  ControlReleaseResponse,
  ControlStopRequest,
  ControlStopResponse,
  LogsResponse,
  OperationStatusResponse,
  StatusResponse,
  VersionResponse,
} from '../types/api';

export class ApiError extends Error {
  constructor(
    message: string,
    public status?: number,
    public code?: string | null,
    public detail?: any
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export class ApiClient {
  private baseUrl: string;
  private timeoutMs: number;

  constructor(baseUrl: string = '', timeoutMs: number = 5000) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.timeoutMs = timeoutMs;
  }

  private async request<T>(
    path: string,
    options: RequestInit = {}
  ): Promise<T> {
    if (typeof navigator !== 'undefined' && !navigator.onLine) {
      throw new ApiError('Client is offline', 0, 'OFFLINE');
    }

    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const headers = new Headers(options.headers || {});
      if (!headers.has('Accept')) {
        headers.set('Accept', 'application/json');
      }
      if (options.body && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }

      const response = await fetch(url, {
        ...options,
        headers,
        signal: controller.signal,
      });

      if (!response.ok) {
        let errData: any = null;
        try {
          errData = await response.json();
        } catch {
          errData = await response.text().catch(() => null);
        }
        const message =
          errData?.detail ||
          errData?.message ||
          `HTTP ${response.status} ${response.statusText}`;
        const code = errData?.error || null;
        throw new ApiError(message, response.status, code, errData);
      }

      return (await response.json()) as T;
    } catch (err: any) {
      if (err.name === 'AbortError') {
        throw new ApiError(`Request timed out after ${this.timeoutMs}ms`, 408, 'TIMEOUT');
      }
      if (err instanceof ApiError) {
        throw err;
      }
      throw new ApiError(err.message || 'Network request failed', 0, 'NETWORK_ERROR', err);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  // API Endpoints

  async getVersion(): Promise<VersionResponse> {
    return this.request<VersionResponse>('/api/v1/version', { method: 'GET' });
  }

  async getStatus(): Promise<StatusResponse> {
    return this.request<StatusResponse>('/api/v1/status', { method: 'GET' });
  }

  async getLogs(limit: number = 50): Promise<LogsResponse> {
    const lim = Math.max(1, Math.min(200, limit));
    return this.request<LogsResponse>(`/api/v1/logs?limit=${lim}`, { method: 'GET' });
  }

  async startController(requestId: string): Promise<OperationStatusResponse> {
    return this.request<OperationStatusResponse>('/api/v1/controller/start', {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, action: 'start' }),
    });
  }

  async stopController(requestId: string): Promise<OperationStatusResponse> {
    return this.request<OperationStatusResponse>('/api/v1/controller/stop', {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, action: 'stop' }),
    });
  }

  async getOperation(id: string): Promise<OperationStatusResponse> {
    return this.request<OperationStatusResponse>(`/api/v1/operations/${encodeURIComponent(id)}`, {
      method: 'GET',
    });
  }

  async acquireControl(req: ControlAcquireRequest): Promise<ControlAcquireResponse> {
    return this.request<ControlAcquireResponse>('/api/v1/control/acquire', {
      method: 'POST',
      body: JSON.stringify(req),
    });
  }

  async releaseControl(req: ControlReleaseRequest): Promise<ControlReleaseResponse> {
    return this.request<ControlReleaseResponse>('/api/v1/control/release', {
      method: 'POST',
      body: JSON.stringify(req),
    });
  }

  async armControl(req: ControlArmRequest): Promise<ControlArmResponse> {
    return this.request<ControlArmResponse>('/api/v1/control/arm', {
      method: 'POST',
      body: JSON.stringify(req),
    });
  }

  async stopControl(req: ControlStopRequest): Promise<ControlStopResponse> {
    return this.request<ControlStopResponse>('/api/v1/control/stop', {
      method: 'POST',
      body: JSON.stringify(req),
    });
  }
}

export const apiClient = new ApiClient();

