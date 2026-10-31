"""Each check of ``deploy/ownvitals-deploy`` is load-bearing: removed or loosened in a copy of the
script, the scenario that covers it fails.

A mutation replaces one exact piece of the script (it must occur exactly once, so a refactor that
moves a check makes this file fail until the table follows). The scenarios come from
``test_deploy_script``, where they also pass on the unmodified script. Synthetic data, fakes only.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from test_deploy_harness import SCRIPT, Host
from test_deploy_script import (
    scenario_deploy,
    scenario_environment_reaches_no_tool,
    scenario_hops_to_root_through_sudo,
    scenario_migration_failure_records_nothing,
    scenario_one_deploy_at_a_time,
    scenario_refuses_a_branch_ruleset,
    scenario_refuses_a_malformed_token,
    scenario_refuses_a_missing_service,
    scenario_refuses_a_ruleset_for_other_tags,
    scenario_refuses_a_service_by_tag,
    scenario_refuses_a_state_file_it_did_not_write,
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
    scenario_state_records_deployed_and_previous,
    scenario_tool_output_is_never_shown,
    scenario_up_failure_keeps_the_rollback_target,
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
    "state line count unchecked": (
        "((${#lines[@]} == 4)) || bad_state",
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
    "previous not recorded": (
        'record "$TAG" "$DIGEST" "$previous_tag" "$previous_digest"',
        'record "$TAG" "$DIGEST" "" ""',
        scenario_state_records_deployed_and_previous,
    ),
    "redeploy drops the rollback target": (
        "previous_tag=$PREVIOUS_TAG previous_digest=$PREVIOUS_DIGEST #",
        ": #",
        scenario_state_records_deployed_and_previous,
    ),
    "recorded only after up -d": (
        '  record "$TAG" "$DIGEST" "$previous_tag" "$previous_digest"\n'
        '  start_release "$DIGEST" "$TAG is recorded (its migrations ran); rollback: '
        '${previous_tag:-none}"\n',
        '  mv -f -- "$OVERRIDE_NEXT" "$OVERRIDE"\n'
        '  start_release "$DIGEST" "up -d failed"\n'
        '  write_state "$TAG" "$DIGEST" "$previous_tag" "$previous_digest"\n',
        scenario_up_failure_keeps_the_rollback_target,
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
    # main
    "main check skipped": ('  check_on_main "$COMMIT"\n', "", scenario_refuses_commit_off_main),
    "diverged accepted": (
        "identical | ahead)",
        "identical | ahead | diverged | behind)",
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
    "pull output shown": (
        'pull --quiet "$IMAGE@$1" > /dev/null 2>&1',
        'pull --quiet "$IMAGE@$1"',
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
        '  record "$tag" "$digest" "" ""\n',
        '  run_migrations\n  record "$tag" "$digest" "" ""\n',
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
