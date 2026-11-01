"""Each check of ``deploy/ownvitals-deploy`` is load-bearing: removed or loosened in a copy of the
script, the scenario that covers it fails.

A mutation replaces one exact piece of the script (it must occur exactly once, so a refactor that
moves a check makes this file fail until the table follows). The scenarios come from
``test_deploy_script``, ``test_deploy_versions`` and ``test_deploy_failures``, where they also pass
on the unmodified script. Synthetic data, fakes only.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from test_deploy_db_functions import (
    scenario_refuses_db_functions_with_a_config,
    scenario_refuses_db_functions_with_a_volume,
    scenario_refuses_db_functions_with_another_secret,
)
from test_deploy_failures import (
    scenario_a_failed_state_write_records_nothing,
    scenario_a_hung_pull_is_stopped,
    scenario_an_override_not_put_in_place_exits_2,
)
from test_deploy_harness import SCRIPT, Host
from test_deploy_script import (
    scenario_deploy,
    scenario_environment_reaches_no_tool,
    scenario_files_are_root_only,
    scenario_hops_to_root_through_sudo,
    scenario_migration_failure_records_nothing,
    scenario_one_deploy_at_a_time,
    scenario_refuses_a_branch_ruleset,
    scenario_refuses_a_malformed_token,
    scenario_refuses_a_missing_service,
    scenario_refuses_a_ruleset_for_other_tags,
    scenario_refuses_a_ruleset_the_owner_may_bypass,
    scenario_refuses_a_service_by_tag,
    scenario_refuses_a_state_file_it_did_not_write,
    scenario_refuses_an_unexpected_ruleset_id,
    scenario_refuses_commit_off_main,
    scenario_refuses_evaluate_mode_ruleset,
    scenario_refuses_excluded_tag,
    scenario_refuses_malformed_commands,
    scenario_refuses_old_gh,
    scenario_refuses_other_source_commit,
    scenario_refuses_other_source_ref,
    scenario_refuses_self_hosted_runner,
    scenario_refuses_unprotected_tag,
    scenario_refuses_without_cert_identity,
    scenario_rollback_refused_when_previous_attestation_is_gone,
    scenario_rollback_runs_every_check,
    scenario_rollback_runs_no_migration,
    scenario_state_records_deployed_and_previous,
    scenario_tool_output_is_never_shown,
    scenario_up_failure_keeps_the_rollback_target,
)
from test_deploy_versions import (
    scenario_accepts_a_newer_release,
    scenario_accepts_the_same_release_again,
    scenario_compares_versions_as_numbers,
    scenario_first_deploy_takes_any_release,
    scenario_no_deploy_below_the_highest_after_a_rollback,
    scenario_refuses_a_higher_tag_on_an_older_commit,
    scenario_refuses_a_release_below_the_highest,
    scenario_refuses_an_older_release,
    scenario_refuses_the_same_release_with_another_digest,
    scenario_rollback_is_not_held_to_the_highest,
)

Scenario = Callable[[Host], None]
# name: (the exact text, its replacement, the scenario that must then fail)
MUTATIONS: dict[str, tuple[str, str, Scenario]] = {
    # arguments
    "rollback with more words": (
        "if [[ $line == rollback ]]; then",
        "if [[ $line == rollback* ]]; then",
        scenario_refuses_malformed_commands,
    ),
    "deploy not anchored": (
        r"^deploy\ ([^ ]+)\ ([^ ]+)$ ]]",
        r"^deploy\ ([^ ]+)\ ([^ ]+) ]]",
        scenario_refuses_malformed_commands,
    ),
    "tag pattern loosened": (
        "[[ $TAG =~ $TAG_RE ]] ||",
        "[[ $TAG =~ ^v[0-9] ]] ||",
        scenario_refuses_malformed_commands,
    ),
    "digest pattern loosened": (
        "[[ $DIGEST =~ $DIGEST_RE ]] ||",
        "[[ $DIGEST =~ ^sha256:[0-9a-fA-F]{64}$ ]] ||",
        scenario_refuses_malformed_commands,
    ),
    "arguments accepted": ("(($# == 0)) ||", "true ||", scenario_refuses_malformed_commands),
    # the environment and the hop to root
    "environment kept": (
        'unset -v "$name" 2> /dev/null || true',
        ":",
        scenario_environment_reaches_no_tool,
    ),
    "not bash -p": ("#!/bin/bash -p\n", "#!/bin/bash\n", scenario_environment_reaches_no_tool),
    "token not given to gh": (
        "GH_TOKEN=$GH_READ_TOKEN GH_CONFIG_DIR",
        "GH_CONFIG_DIR",
        scenario_environment_reaches_no_tool,
    ),
    "token not checked": (
        "[[ $token =~ $TOKEN_RE ]] ||",
        "true ||",
        scenario_refuses_a_malformed_token,
    ),
    "no root check": (
        "if ((EUID != ROOT_UID)); then",
        "if false; then",
        scenario_hops_to_root_through_sudo,
    ),
    "sudo may prompt": ("exec sudo -n -- ", "exec sudo -- ", scenario_hops_to_root_through_sudo),
    "command not kept for root": (
        "SSH_ORIGINAL_COMMAND=$line exec sudo",
        "exec sudo",
        scenario_hops_to_root_through_sudo,
    ),
    # lock and state
    "no lock": (
        'flock --nonblock --conflict-exit-code 75 "$LOCK_FD" ||',
        "true ||",
        scenario_one_deploy_at_a_time,
    ),
    "umask not set": ("umask 077\n", "", scenario_files_are_root_only),
    "state line count unchecked": (
        "((${#lines[@]} == ${#keys[@]})) || bad_state",
        ":",
        scenario_refuses_a_state_file_it_did_not_write,
    ),
    "state current unchecked": (
        "[[ $CURRENT_TAG =~ $TAG_RE && $CURRENT_DIGEST =~ $DIGEST_RE ]] || bad_state",
        ":",
        scenario_refuses_a_state_file_it_did_not_write,
    ),
    "state previous unchecked": (
        "[[ $PREVIOUS_TAG =~ $TAG_RE && $PREVIOUS_DIGEST =~ $DIGEST_RE ]] || bad_state",
        ":",
        scenario_refuses_a_state_file_it_did_not_write,
    ),
    "state highest unchecked": (
        "  [[ $HIGHEST_TAG =~ $TAG_RE && $HIGHEST_COMMIT =~ $COMMIT_RE && $HIGHEST_DIGEST =~ "
        "$DIGEST_RE ]] \\\n    || bad_state\n",
        "",
        scenario_refuses_a_state_file_it_did_not_write,
    ),
    "previous not recorded": (
        "    PREVIOUS_TAG=$CURRENT_TAG PREVIOUS_DIGEST=$CURRENT_DIGEST # a redeploy",
        "    : #",
        scenario_state_records_deployed_and_previous,
    ),
    "redeploy drops the rollback target": (
        'if [[ $TAG != "$CURRENT_TAG" || $DIGEST != "$CURRENT_DIGEST" ]]; then',
        "if true; then",
        scenario_state_records_deployed_and_previous,
    ),
    "recorded only after up -d": (
        '  record "$recorded"\n  start_release "$DIGEST" "$recorded"\n',
        '  cat "$OVERRIDE_NEXT" > "$OVERRIDE"\n'
        '  start_release "$DIGEST" "up -d failed"\n'
        '  record "$recorded"\n',
        scenario_up_failure_keeps_the_rollback_target,
    ),
    "state write unchecked": (
        'write_state || refuse "state: $STATE_FILE could not be written"',
        "write_state",
        scenario_a_failed_state_write_records_nothing,
    ),
    "override failure exits 1": (
        '  if ! mv -fT -- "$OVERRIDE_NEXT" "$OVERRIDE" 2> /dev/null; then\n',
        '  mv -fT -- "$OVERRIDE_NEXT" "$OVERRIDE"\n  if false; then\n',
        scenario_an_override_not_put_in_place_exits_2,
    ),
    "override moved into a directory": (
        'mv -fT -- "$OVERRIDE_NEXT"',
        'mv -f -- "$OVERRIDE_NEXT"',
        scenario_an_override_not_put_in_place_exits_2,
    ),
    # never below the highest release deployed
    "version check skipped": ("  check_version\n", "", scenario_refuses_an_older_release),
    "older release accepted": (
        'refuse "version: $TAG is below the highest deployed $HIGHEST_TAG; only rollback goes',
        'ok "version: older"',
        scenario_refuses_an_older_release,
    ),
    "compared with the current release": (
        'elif version_newer "$TAG" "$HIGHEST_TAG"; then',
        'elif version_newer "$TAG" "$CURRENT_TAG"; then',
        scenario_refuses_a_release_below_the_highest,
    ),
    "compared with the previous release": (
        'elif version_newer "$TAG" "$HIGHEST_TAG"; then',
        'elif version_newer "$TAG" "$PREVIOUS_TAG"; then',
        scenario_refuses_a_release_below_the_highest,
    ),
    "highest not recorded": (
        "  HIGHEST_TAG=$TAG HIGHEST_COMMIT=$COMMIT HIGHEST_DIGEST=$DIGEST #",
        "  : #",
        scenario_state_records_deployed_and_previous,
    ),
    "rollback lowers the highest": (
        "  CURRENT_TAG=$tag CURRENT_DIGEST=$digest PREVIOUS_TAG='' PREVIOUS_DIGEST=''\n",
        "  CURRENT_TAG=$tag CURRENT_DIGEST=$digest PREVIOUS_TAG='' PREVIOUS_DIGEST=''\n"
        "  HIGHEST_TAG=$tag HIGHEST_COMMIT=$COMMIT HIGHEST_DIGEST=$digest\n",
        scenario_no_deploy_below_the_highest_after_a_rollback,
    ),
    "history unchecked": (
        '  if [[ $ACTION == deploy && -n $HIGHEST_TAG ]]; then check_history "$COMMIT"; fi\n',
        "",
        scenario_refuses_a_higher_tag_on_an_older_commit,
    ),
    "history accepts an older commit": (
        'identical | ahead) ok "history:',
        'identical | ahead | behind) ok "history:',
        scenario_refuses_a_higher_tag_on_an_older_commit,
    ),
    "rollback held to the history": (
        "[[ $ACTION == deploy && -n $HIGHEST_TAG ]]",
        "[[ -n $HIGHEST_TAG ]]",
        scenario_rollback_is_not_held_to_the_highest,
    ),
    "same version, any digest": (
        '    [[ $DIGEST == "$HIGHEST_DIGEST" ]] \\\n',
        "    true \\\n",
        scenario_refuses_the_same_release_with_another_digest,
    ),
    "same release refused": (
        'ok "version: $TAG again, with its recorded digest"',
        'refuse "version: again"',
        scenario_accepts_the_same_release_again,
    ),
    "newer release refused": (
        "[[ ${a[i]} > ${b[i]} ]]",
        "[[ ${a[i]} < ${b[i]} ]]",
        scenario_accepts_a_newer_release,
    ),
    "versions compared as text": (
        "    if ((${#a[i]} != ${#b[i]})); then ((${#a[i]} > ${#b[i]})); return; fi\n",
        "",
        scenario_compares_versions_as_numbers,
    ),
    "first deploy refused": (
        'ok "version: no deployment recorded; any release may be the first"',
        'refuse "version: none recorded"',
        scenario_first_deploy_takes_any_release,
    ),
    # gh
    "gh version unchecked": (
        'version_at_least "$major" "$minor" "$patch" \\\n',
        'true "$major" "$minor" "$patch" \\\n',
        scenario_refuses_old_gh,
    ),
    # the tag
    "tag by bare name": ("commits/refs/tags/$1", "commits/$1", scenario_deploy),
    "ruleset check skipped": (
        '  check_tag_protected "$1"\n',
        '  : "$1"\n',
        scenario_refuses_unprotected_tag,
    ),
    "ruleset without deletion": (
        "[[ $update == true && $deletion == true ]]",
        "[[ $update == true ]]",
        scenario_refuses_unprotected_tag,
    ),
    "ruleset in evaluate mode": (
        "&& $enforcement == '\"active\"' ",
        "",
        scenario_refuses_evaluate_mode_ruleset,
    ),
    "ruleset for branches": (
        "[[ $target == '\"tag\"' && ",
        "[[ ",
        scenario_refuses_a_branch_ruleset,
    ),
    "ruleset for other tags": (
        "then covered=true; fi",
        "then :; fi; covered=true",
        scenario_refuses_a_ruleset_for_other_tags,
    ),
    "excludes ignored": (
        " && $excluded == false ]]",
        " ]]",
        scenario_refuses_excluded_tag,
    ),
    "bypass ignored": (
        " && [[ $bypass == '\"never\"' ]]",
        "",
        scenario_refuses_a_ruleset_the_owner_may_bypass,
    ),
    "ruleset id unchecked": (
        '[[ $id =~ ^[0-9]{1,19}$ ]] || refuse "tag: the API returned an unexpected ruleset id"',
        ":",
        scenario_refuses_an_unexpected_ruleset_id,
    ),
    # main
    "main check skipped": ('  check_on_main "$COMMIT"\n', "", scenario_refuses_commit_off_main),
    "diverged accepted": (
        'identical | ahead) ok "main:',
        'identical | ahead | diverged | behind) ok "main:',
        scenario_refuses_commit_off_main,
    ),
    "bare main": ("...refs/heads/main?per_page=1", "...main?per_page=1", scenario_deploy),
    # the attestation
    "attestation skipped": (
        '  check_attestation "$1" "$2" "$COMMIT"\n',
        "",
        scenario_refuses_without_cert_identity,
    ),
    "no cert identity": (
        '    --cert-identity "$SIGNER@refs/tags/$tag" \\\n',
        "",
        scenario_refuses_without_cert_identity,
    ),
    "no source ref": (
        '    --source-ref "refs/tags/$tag" \\\n',
        "",
        scenario_refuses_other_source_ref,
    ),
    "no source digest": (
        '    --source-digest "$commit" \\\n',
        "",
        scenario_refuses_other_source_commit,
    ),
    "self-hosted runners allowed": (
        "    --deny-self-hosted-runners \\\n",
        "",
        scenario_refuses_self_hosted_runner,
    ),
    # pull, compose, migrations, start
    "pull by tag": ('pull --quiet "$IMAGE@$1"', 'pull --quiet "$IMAGE:latest"', scenario_deploy),
    "pull without a timeout": (
        'timeout "$PULL_TIMEOUT" docker pull',
        "docker pull",
        scenario_a_hung_pull_is_stopped,
    ),
    "pull output shown": (
        'pull --quiet "$IMAGE@$1" > /dev/null 2>&1',
        'pull --quiet "$IMAGE@$1"',
        scenario_tool_output_is_never_shown,
    ),
    "gh api errors shown": (
        '--jq "$2" 2> /dev/null; }',
        '--jq "$2"; }',
        scenario_tool_output_is_never_shown,
    ),
    "config --services output shown": (
        "config --services 2> /dev/null)",
        "config --services)",
        scenario_tool_output_is_never_shown,
    ),
    "config --images output shown": (
        "config --images 2> /dev/null)",
        "config --images)",
        scenario_tool_output_is_never_shown,
    ),
    "attestation output shown": (
        '--predicate-type "$PREDICATE_TYPE" > /dev/null 2>&1; then',
        '--predicate-type "$PREDICATE_TYPE"; then',
        scenario_tool_output_is_never_shown,
    ),
    "migration output shown": (
        "--pull never migrate > /dev/null 2>&1; then",
        "--pull never migrate; then",
        scenario_tool_output_is_never_shown,
    ),
    "up may pull": (
        "up -d --pull never --no-build",
        "up -d --no-build",
        scenario_deploy,
    ),
    "services unchecked": (
        '|| refuse "compose: $COMPOSE_BASE has no service $service"',
        "|| :",
        scenario_refuses_a_missing_service,
    ),
    "db-functions mounts unchecked": (
        "  check_db_functions_mounts\n",
        "",
        scenario_refuses_db_functions_with_a_volume,
    ),
    "db-functions volumes accepted": (
        'refuse "compose: db-functions has $key; its SQL must come from the image only" ;;',
        ": ;;",
        scenario_refuses_db_functions_with_a_volume,
    ),
    "db-functions configs accepted": (
        "volumes | volumes_from | configs | develop)",
        "volumes | volumes_from | develop)",
        scenario_refuses_db_functions_with_a_config,
    ),
    "db-functions secrets unchecked": (
        '  [[ $secrets == "$DB_FUNCTIONS_SECRETS" ]] \\\n',
        "  true \\\n",
        scenario_refuses_db_functions_with_another_secret,
    ),
    "config output shown": (
        'config=$(compose "$OVERRIDE_NEXT" config 2> /dev/null)',
        'config=$(compose "$OVERRIDE_NEXT" config)',
        scenario_tool_output_is_never_shown,
    ),
    "image by tag accepted": (
        'refuse "compose: a service names $IMAGE other than by the digest $2"',
        ":",
        scenario_refuses_a_service_by_tag,
    ),
    "override may pull": (
        "    pull_policy: never\\n",
        "    pull_policy: always\\n",
        scenario_deploy,
    ),
    "migrate not scaled to 0": (
        "      if [[ $service == migrate ]]; then printf '    scale: 0\\n'; fi\n",
        "",
        scenario_rollback_runs_no_migration,
    ),
    "every service scaled to 0": (
        "if [[ $service == migrate ]]; then printf",
        "if true; then printf",
        scenario_deploy,
    ),
    "migrations skipped": ("  run_migrations\n", "", scenario_deploy),
    "failed migrations ignored": (
        'refuse "migrations: the migrate container of $TAG failed"',
        ":",
        scenario_migration_failure_records_nothing,
    ),
    # rollback
    "rollback unchecked": (
        '  verify_release "$tag" "$digest"\n',
        "",
        scenario_rollback_refused_when_previous_attestation_is_gone,
    ),
    "rollback migrates": (
        '  record "$recorded"\n  start_release "$digest" "$recorded"\n',
        '  run_migrations\n  record "$recorded"\n  start_release "$digest" "$recorded"\n',
        scenario_rollback_runs_every_check,
    ),
}


def mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the mutation's text must occur exactly once: {old!r}"
    return text.replace(old, new)


@pytest.mark.parametrize("name", MUTATIONS)
def test_removing_a_check_fails_its_scenario(tmp_path: Path, name: str) -> None:
    old, new, scenario = MUTATIONS[name]
    script = mutate(SCRIPT.read_text("utf-8"), old, new)
    root_uid = "-1" if scenario is scenario_hops_to_root_through_sudo else None
    host = Host(tmp_path, script=script, root_uid=root_uid)
    with pytest.raises(AssertionError):
        scenario(host)
