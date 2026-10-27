import { Body, Controller, Get, Param, Post, UseGuards, BadRequestException } from '@nestjs/common';
import { AuthGuard } from '../auth/auth.guard';
import { DockerService } from './docker.service';
import type { AppUrn } from '@runtipi/common/types';
import type { Grant } from './wizard-storage';

@Controller('apps')
@UseGuards(AuthGuard)
export class WizardSharesController {
  constructor(private readonly docker: DockerService) {}
  @Get(':id/proposed-grants')
  proposed(@Param('id') id: string) {
    return this.docker.storage.proposed(id.split(':')[0]);
  }
  @Get('wizard/grants')
  async grants() {
    return (await this.docker.storage.readGrants()).grants;
  }
  @Post(':id/app-grants')
  async appChange(@Param('id') id: string, @Body() body: { to: string; approve: boolean }) {
    if (typeof body.approve !== 'boolean' || typeof body.to !== 'string') throw new BadRequestException('to and approve required');
    await this.docker.storage.change({ kind: 'app', from: id.split(':')[0]!, to: body.to }, body.approve);
    return { grants: (await this.docker.storage.readGrants()).grants, connections: await this.docker.storage.connections() };
  }
  // Owner-only connection controls (this controller is AuthGuard'ed: the App Manager's owner sign-in). Apps can't
  // call these; an app can only ask, through its own /wizard-requests/connections.json.
  @Get('wizard/connections')
  async connections() {
    return this.docker.storage.connections();
  }
  @Post(':id/sensitive')
  async sensitive(@Param('id') id: string, @Body() body: { sensitive: boolean }) {
    if (typeof body?.sensitive !== 'boolean') throw new BadRequestException('sensitive must be boolean');
    return this.docker.storage.setSensitive(id.split(':')[0]!, body.sensitive);
  }
  @Post('wizard/connection-notices')
  async notices(@Body() body: { delivered: string[] }) {
    if (!Array.isArray(body?.delivered)) throw new BadRequestException('delivered must be a list of notice ids');
    return this.docker.storage.markNotices(body.delivered);
  }
  @Post(':id/share-grants')
  async change(@Param('id') id: string, @Body() body: { to: string; share: string; approve: boolean }) {
    if (typeof body.approve !== 'boolean') throw new BadRequestException('approve must be boolean');
    const grant: Grant = { kind: 'share', from: id.split(':')[0]!, to: body.to, share: body.share };
    const urn = await this.docker.storage.change(grant, body.approve);
    // Recreate, do not merely restart: Docker restart retains the old mount namespace.
    // Failure is surfaced; revoke is durable even if recreation needs retry.
    await this.docker.composeApp(urn as AppUrn, 'down --remove-orphans');
    await this.docker.composeApp(urn as AppUrn, 'up --detach --force-recreate --remove-orphans');
    return { grants: (await this.docker.storage.readGrants()).grants };
  }
}
