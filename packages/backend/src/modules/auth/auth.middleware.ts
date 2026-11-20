import { FORWARD_AUTH_COOKIE_NAME, SESSION_COOKIE_NAME } from '@/common/constants';
import { CacheService } from '@/core/cache/cache.service';
import { ConfigurationService } from '@/core/config/configuration.service';
import { Injectable, type NestMiddleware } from '@nestjs/common';
import type { NextFunction, Request, Response } from 'express';
import jsonwebtoken from 'jsonwebtoken';
import { UserRepository } from '../user/user.repository';

type ForwardAuthSession = {
  sessionId: string;
  host: string;
};

/**
 * Wizard: CLI tokens are signed with the private CLI key (Runtipi 4.12). Until every Wizard tool reads that key, a
 * token signed with the old JWT_SECRET is still accepted (what Runtipi 4.10 did). WIZARD_LEGACY_CLI_TOKENS=0 turns
 * the fallback off.
 */
export const verifyCliToken = (token: string, cliSecret: string, jwtSecret: string | undefined) => {
  try {
    return jsonwebtoken.verify(token, cliSecret, { algorithms: ['HS256'] });
  } catch (error) {
    if (process.env.WIZARD_LEGACY_CLI_TOKENS === '0' || !jwtSecret) throw error;
    return jsonwebtoken.verify(token, jwtSecret, { algorithms: ['HS256'] });
  }
};

@Injectable()
export class AuthMiddleware implements NestMiddleware {
  constructor(
    private readonly cache: CacheService,
    private readonly config: ConfigurationService,
    private readonly userRepository: UserRepository,
  ) {}

  private isTraefikAuthRequest(req: Request) {
    return [req.path, req.originalUrl, req.url].some((url) => url?.split('?')[0]?.endsWith('/auth/traefik'));
  }

  private getForwardedHost(req: Request) {
    return (req.headers['x-forwarded-host'] as string | undefined)?.split(',')[0]?.trim().split(':')[0]?.toLowerCase();
  }

  private getForwardAuthSession(token: string): ForwardAuthSession | null {
    try {
      const session = JSON.parse(token) as ForwardAuthSession;
      if (!session.sessionId || !session.host) {
        return null;
      }

      return { sessionId: session.sessionId, host: session.host.toLowerCase() };
    } catch {
      return null;
    }
  }

  async use(req: Request, _: Response, next: NextFunction) {
    const sessionId = req.cookies[SESSION_COOKIE_NAME];
    const forwardAuthSessionId = req.cookies[FORWARD_AUTH_COOKIE_NAME];
    const bearerToken = req.headers.authorization;
    const isTraefikAuthRequest = this.isTraefikAuthRequest(req);

    if (forwardAuthSessionId && isTraefikAuthRequest) {
      const forwardAuthSession = this.getForwardAuthSession(this.cache.get(`forward-auth-session:${forwardAuthSessionId}`) ?? '');
      const forwardedHost = this.getForwardedHost(req);
      const userId =
        forwardAuthSession && forwardedHost && forwardAuthSession.host === forwardedHost
          ? this.cache.get(`session:${forwardAuthSession.sessionId}`)
          : undefined;
      const numericUserId = Number(userId);

      if (!Number.isNaN(numericUserId)) {
        const user = await this.userRepository.getUserDtoById(numericUserId);
        req.user = user;
        req.authMethod = 'forward-auth';
      }

      return next();
    }

    if (sessionId && !isTraefikAuthRequest) {
      const userId = this.cache.get(`session:${sessionId}`);
      if (!Number.isNaN(Number(userId))) {
        const user = await this.userRepository.getUserDtoById(Number(userId));
        req.user = user;
        req.authMethod = 'session';
      }

      return next();
    }

    if (bearerToken) {
      const token = bearerToken.split(' ')[1];

      if (!token) {
        return next();
      }

      const cliSecret = this.config.get('cliSecret');

      try {
        const { sub } = verifyCliToken(token, cliSecret, this.config.get('jwtSecret')) as { sub: string };
        if (sub === 'cli') {
          const user = await this.userRepository.getFirstOperator();
          req.user = user;
          req.authMethod = 'cli';
        }

        return next();
      } catch (_error) {
        return next();
      }
    }

    return next();
  }
}
