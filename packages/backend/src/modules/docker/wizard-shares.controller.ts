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
