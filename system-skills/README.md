# System skills (the one home)

This folder is the only place WizardOS system skills are written (wizard-os#760). Every box gets them from here:
the builder bundle (`wam builder-bundle install`, run by the installer and by updates) copies this folder into
`~/.wizard-os/system-skills` and registers it as one `skills.external_dirs` entry. The stock container mounts the same
copy at `/opt/wizard-builder/current/system-skills`.

- `wizard-builder`: build, port or change an app (wam's executable workflow; it also carries the shared-style rules
  that the retired wizard-os `wizard-app-builder` pointer used to repeat).
- `wizard-guide`: how to use Wizard.

The skills live next to `wam` because they drive its commands, so a skill and the tool it names always ship in the
same release. Contracts and style stay in wizard-os (`spec/`, `style/`); link to them, never copy them here as a second
authored source. Do not add a `system-skills/` folder in another repo.
