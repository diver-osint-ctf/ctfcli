import logging
import re
import subprocess
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import click
import yaml
from cookiecutter.main import cookiecutter
from slugify import slugify

from ctfcli.core.api import API
from ctfcli.core.exceptions import (
    ChallengeException,
    InvalidChallengeDefinition,
    InvalidChallengeFile,
    LintException,
    RemoteChallengeNotFound,
)
from ctfcli.core.image import Image
from ctfcli.utils.hashing import hash_file
from ctfcli.utils.tools import strings

log = logging.getLogger("ctfcli.core.challenge")


def str_presenter(dumper, data):
    if len(data.splitlines()) > 1 or "\n" in data:
        text_list = [line.rstrip() for line in data.splitlines()]
        fixed_data = "\n".join(text_list)
        return dumper.represent_scalar("tag:yaml.org,2002:str", fixed_data, style="|")
    elif len(data) > 80:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data.rstrip(), style=">")

    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(str, str_presenter)


class Challenge(dict):
    key_order = [
        # fmt: off
        "name", "author", "category", "description", "attribution", "value",
        "type", "extra", "image", "protocol", "host",
        "connection_info", "healthcheck", "attempts", "flags", "geo_flags",
        "files", "topics", "tags", "files", "hints",
        "requirements", "next", "state", "version",
        # fmt: on
    ]

    keys_with_newline = [
        # fmt: off
        "extra", "image", "attempts", "flags", "geo_flags", "topics", "tags",
        "files", "hints", "requirements", "state", "version"
        # fmt: on
    ]

    @staticmethod
    def load_installed_challenge(challenge_id) -> Dict:
        api = API()
        r = api.get(f"/api/v1/challenges/{challenge_id}?view=admin")

        if not r.ok:
            raise RemoteChallengeNotFound(f"Could not load challenge with id={challenge_id}")

        installed_challenge = r.json().get("data", None)
        if not installed_challenge:
            raise RemoteChallengeNotFound(f"Could not load challenge with id={challenge_id}")

        return installed_challenge

    @staticmethod
    def load_installed_challenges() -> List:
        api = API()
        r = api.get("/api/v1/challenges?view=admin")

        if not r.ok:
            return []

        installed_challenges = r.json().get("data", None)
        if not installed_challenges:
            return []

        return installed_challenges

    @staticmethod
    def is_default_challenge_property(key: str, value: Any) -> bool:
        if key == "connection_info" and value is None:
            return True

        if key == "attempts" and value == 0:
            return True

        if key == "state" and value == "visible":
            return True

        if key == "type" and value == "standard":
            return True

        if key in ["tags", "hints", "topics", "requirements", "files"] and value == []:
            return True

        if key == "requirements" and value == {"prerequisites": [], "anonymize": False}:
            return True

        if key == "next" and value is None:
            return True
        return False

    @staticmethod
    def clone(config, remote_challenge):
        name = remote_challenge["name"]

        if name is None:
            raise ChallengeException(f'Could not get name of remote challenge with id {remote_challenge["id"]}')

        # First, generate a name for the challenge directory
        category = remote_challenge.get("category", None)
        challenge_dir_name = slugify(name)
        if category is not None:
            challenge_dir_name = str(Path(slugify(category)) / challenge_dir_name)

        if Path(challenge_dir_name).exists():
            raise ChallengeException(
                f"Challenge directory '{challenge_dir_name}' for challenge '{name}' already exists"
            )

        # Create an blank/empty challenge, with only the challenge.yml containing the challenge name
        template_path = config.get_base_path() / "templates" / "blank" / "empty"
        log.debug(f"Challenge.clone: cookiecutter({str(template_path)}, {name=}, {challenge_dir_name=}")
        cookiecutter(
            str(template_path),
            no_input=True,
            extra_context={"name": name, "dirname": challenge_dir_name},
        )

        if not Path(challenge_dir_name).exists():
            raise ChallengeException(f"Could not create challenge directory '{challenge_dir_name}' for '{name}'")

        # Add the newly created local challenge to the config file
        config["challenges"][challenge_dir_name] = challenge_dir_name
        with open(config.config_path, "w+") as f:
            config.write(f)

        return Challenge(f"{challenge_dir_name}/challenge.yml")

    @property
    def api(self):
        if not self._api:
            self._api = API()

        return self._api

    # __init__ expects an absolute path to challenge_yml, or a relative one from the cwd
    # it does not join that path with the project_path
    def __init__(self, challenge_yml: Union[str, PathLike], overrides=None):
        log.debug(f"Challenge.__init__: ({challenge_yml=}, {overrides=}")
        if overrides is None:
            overrides = {}

        self.challenge_file_path = Path(challenge_yml)

        if not self.challenge_file_path.is_file():
            raise InvalidChallengeFile(f"Challenge file at {self.challenge_file_path} could not be found")

        self.challenge_directory = self.challenge_file_path.parent

        with open(self.challenge_file_path) as challenge_file:
            try:
                challenge_definition = yaml.safe_load(challenge_file.read())
            except yaml.YAMLError as e:
                raise InvalidChallengeFile(f"Challenge file at {self.challenge_file_path} could not be loaded:\n{e}")

            if type(challenge_definition) != dict:
                raise InvalidChallengeFile(
                    f"Challenge file at {self.challenge_file_path} is either empty or not a dictionary / object"
                )

        challenge_data = {**challenge_definition, **overrides}
        super(Challenge, self).__init__(challenge_data)

        # Challenge id is unknown before loading the remote challenge
        self.challenge_id = None

        # API is not initialized before running an API-related operation, but should be reused later
        self._api = None

        # Assign an image if the challenge provides one, otherwise this will be set to None
        self.image = self._process_challenge_image(self.get("image"))

    def __str__(self):
        return self["name"]

    def _process_challenge_image(self, challenge_image: Optional[str]) -> Optional[Image]:
        if not challenge_image:
            return None

        # Check if challenge_image is explicitly marked with registry:// prefix
        if challenge_image.startswith("registry://"):
            challenge_image = challenge_image.replace("registry://", "")
            return Image(challenge_image)

        # Check if it's a library image
        if challenge_image.startswith("library/"):
            return Image(f"docker.io/{challenge_image}")

        # Check if it defines a known registry
        known_registries = [
            "docker.io",
            "gcr.io",
            "ecr.aws",
            "ghcr.io",
            "azurecr.io",
            "registry.digitalocean.com",
            "registry.gitlab.com",
            "registry.ctfd.io",
        ]
        for registry in known_registries:
            if registry in challenge_image:
                return Image(challenge_image)

        # Check if it's a path to dockerfile to be built
        if (self.challenge_directory / challenge_image / "Dockerfile").exists():
            return Image(slugify(self["name"]), self.challenge_directory / self["image"])

        # Check if it's a local pre-built image
        if (
            subprocess.call(
                ["docker", "inspect", challenge_image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        ):
            return Image(challenge_image)

        # If the image is set, but we fail to determine whether it's local / remote - raise an exception
        raise InvalidChallengeFile(
            f"Challenge file at {self.challenge_file_path} defines an image, but it couldn't be resolved"
        )

    def _load_challenge_id(self):
        remote_challenges = self.load_installed_challenges()
        if not remote_challenges:
            raise RemoteChallengeNotFound("Could not load any remote challenges")

        # get challenge id from the remote
        for inspected_challenge in remote_challenges:
            if inspected_challenge["name"] == self["name"]:
                self.challenge_id = inspected_challenge["id"]
                break

        # return if we failed to determine the challenge id (failed to find the challenge)
        if self.challenge_id is None:
            raise RemoteChallengeNotFound(f"Could not load remote challenge with name '{self['name']}'")

    def _validate_files(self):
        files = self.get("files") or []
        for challenge_file in files:
            if not (self.challenge_directory / challenge_file).exists():
                raise InvalidChallengeFile(f"File {challenge_file} could not be loaded")

    def _get_initial_challenge_payload(self, ignore: Tuple[str] = ()) -> Dict:
        challenge = self
        challenge_payload = {
            "name": self["name"],
            "category": self.get("category", ""),
            "description": self.get("description", ""),
            "attribution": self.get("attribution", ""),
            "type": self.get("type", "standard"),
            # Hide the challenge for the duration of the sync / creation
            "state": "hidden",
        }

        # Some challenge types (e.g., dynamic) override value.
        # We can't send it to CTFd because we don't know the current value
        if challenge.get("value", None) is not None:
            # if value is an int as string, cast it
            if type(challenge["value"]) == str and challenge["value"].isdigit():
                challenge_payload["value"] = int(challenge["value"])

            if type(challenge["value"] == int):
                challenge_payload["value"] = challenge["value"]

        if "attempts" not in ignore:
            challenge_payload["max_attempts"] = challenge.get("attempts", 0)

        if "connection_info" not in ignore:
            challenge_payload["connection_info"] = challenge.get("connection_info", None)

        if "extra" not in ignore:
            challenge_payload = {**challenge_payload, **challenge.get("extra", {})}
        
        if "geo_flags" not in ignore:
            challenge_payload = {**challenge_payload, **challenge.get("geo_flags", {})}

        return challenge_payload

    def _delete_existing_flags(self):
        remote_flags = self.api.get("/api/v1/flags").json()["data"]
        for flag in remote_flags:
            if flag["challenge_id"] == self.challenge_id:
                r = self.api.delete(f"/api/v1/flags/{flag['id']}")
                r.raise_for_status()

    def _create_flags(self):
        for flag in self["flags"]:
            if type(flag) == str:
                flag_payload = {
                    "content": flag,
                    "type": "static",
                    "challenge_id": self.challenge_id,
                }
            else:
                flag_payload = {**flag, "challenge_id": self.challenge_id}

            r = self.api.post("/api/v1/flags", json=flag_payload)
            r.raise_for_status()

    def _delete_existing_topics(self):
        remote_topics = self.api.get(f"/api/v1/challenges/{self.challenge_id}/topics").json()["data"]
        for topic in remote_topics:
            r = self.api.delete(f"/api/v1/topics?type=challenge&target_id={topic['id']}")
            r.raise_for_status()

    def _create_topics(self):
        for topic in self["topics"]:
            r = self.api.post(
                "/api/v1/topics",
                json={
                    "value": topic,
                    "type": "challenge",
                    "challenge_id": self.challenge_id,
                },
            )
            r.raise_for_status()

    def _delete_existing_tags(self):
        remote_tags = self.api.get("/api/v1/tags").json()["data"]
        for tag in remote_tags:
            if tag["challenge_id"] == self.challenge_id:
                r = self.api.delete(f"/api/v1/tags/{tag['id']}")
                r.raise_for_status()

    def _create_tags(self):
        for tag in self["tags"]:
            r = self.api.post(
                "/api/v1/tags",
                json={"challenge_id": self.challenge_id, "value": tag},
            )
            r.raise_for_status()

    def _delete_file(self, remote_location: str):
        remote_files = self.api.get("/api/v1/files?type=challenge").json()["data"]

        for remote_file in remote_files:
            if remote_file["location"] == remote_location:
                r = self.api.delete(f"/api/v1/files/{remote_file['id']}")
                r.raise_for_status()

    def _create_file(self, local_path: Path):
        new_file = ("file", open(local_path, mode="rb"))
        file_payload = {"challenge_id": self.challenge_id, "type": "challenge"}

        # Specifically use data= here to send multipart/form-data
        r = self.api.post("/api/v1/files", files=[new_file], data=file_payload)
        r.raise_for_status()

        # Close the file handle
        new_file[1].close()

    def _create_all_files(self):
        new_files = []

        files = self.get("files") or []
        for challenge_file in files:
            new_files.append(("file", open(self.challenge_directory / challenge_file, mode="rb")))

        files_payload = {"challenge_id": self.challenge_id, "type": "challenge"}

        # Specifically use data= here to send multipart/form-data
        r = self.api.post("/api/v1/files", files=new_files, data=files_payload)
        r.raise_for_status()

        # Close the file handles
        for file_payload in new_files:
            file_payload[1].close()

    def _delete_existing_hints(self):
        remote_hints = self.api.get("/api/v1/hints").json()["data"]
        for hint in remote_hints:
            if hint["challenge_id"] == self.challenge_id:
                r = self.api.delete(f"/api/v1/hints/{hint['id']}")
                r.raise_for_status()

    def _create_hints(self):
        for hint in self["hints"]:
            if type(hint) == str:
                hint_payload = {
                    "content": hint,
                    "title": "",
                    "cost": 0,
                    "challenge_id": self.challenge_id,
                }
            else:
                hint_payload = {
                    "content": hint["content"],
                    "title": hint.get("title", ""),
                    "cost": hint.get("cost", 0),
                    "challenge_id": self.challenge_id,
                }

            r = self.api.post("/api/v1/hints", json=hint_payload)
            r.raise_for_status()

    def _set_required_challenges(self):
        remote_challenges = self.load_installed_challenges()
        required_challenges = []
        anonymize = False
        if type(self["requirements"]) == dict:
            rc = self["requirements"].get("prerequisites", [])
            anonymize = self["requirements"].get("anonymize", False)
        else:
            rc = self["requirements"]

        for required_challenge in rc:
            if type(required_challenge) == str:
                # requirement by name
                # find the challenge id from installed challenges
                found = False
                for remote_challenge in remote_challenges:
                    if remote_challenge["name"] == required_challenge:
                        required_challenges.append(remote_challenge["id"])
                        found = True
                        break
                if found is False:
                    click.secho(
                        f'Challenge id cannot be found. Skipping invalid requirement name "{required_challenge}".',
                        fg="yellow",
                    )

            elif type(required_challenge) == int:
                # requirement by challenge id
                # trust it and use it directly
                required_challenges.append(required_challenge)

        required_challenge_ids = list(set(required_challenges))

        if self.challenge_id in required_challenge_ids:
            click.secho(
                "Challenge cannot require itself. Skipping invalid requirement.",
                fg="yellow",
            )
            required_challenges.remove(self.challenge_id)
        required_challenges.sort()

        requirements_payload = {
            "requirements": {
                "prerequisites": required_challenges,
                "anonymize": anonymize,
            }
        }
        r = self.api.patch(f"/api/v1/challenges/{self.challenge_id}", json=requirements_payload)
        r.raise_for_status()

    def _set_next(self, _next):
        if type(_next) == str:
            # nid by name
            # find the challenge id from installed challenges
            remote_challenges = self.load_installed_challenges()
            for remote_challenge in remote_challenges:
                if remote_challenge["name"] == _next:
                    _next = remote_challenge["id"]
                    break
            if type(_next) == str:
                click.secho(
                    "Challenge cannot find next challenge. Maybe it is invalid name or id. It will be cleared.",
                    fg="yellow",
                )
                _next = None
        elif type(_next) == int and _next > 0:
            # nid by challenge id
            # trust it and use it directly
            _next = remote_challenge["id"]
        else:
            _next = None

        if self.challenge_id == _next:
            click.secho(
                "Challenge cannot set next challenge itself. Skipping invalid next challenge.",
                fg="yellow",
            )
            _next = None

        next_payload = {"next_id": _next}
        r = self.api.patch(f"/api/v1/challenges/{self.challenge_id}", json=next_payload)
        r.raise_for_status()

    # Compare challenge requirements, will resolve all IDs to names
    def _compare_challenge_requirements(self, r1: List[Union[str, int]], r2: List[Union[str, int]]) -> bool:
        remote_challenges = self.load_installed_challenges()

        def normalize_requirements(requirements):
            normalized = []
            for r in requirements:
                if type(r) == int:
                    for remote_challenge in remote_challenges:
                        if remote_challenge["id"] == r:
                            normalized.append(remote_challenge["name"])
                            break
                else:
                    normalized.append(r)

            return normalized

        nr1 = normalize_requirements(r1)
        nr1.sort()
        nr2 = normalize_requirements(r2)
        nr2.sort()
        return nr1 == nr2

    # Compare next challenges, will resolve all IDs to names
    def _compare_challenge_next(self, r1: Union[str, int, None], r2: Union[str, int, None]) -> bool:
        def normalize_next(r):
            normalized = None
            if type(r) == int:
                if r > 0:
                    remote_challenge = self.load_installed_challenge(r)
                    if remote_challenge["id"] == r:
                        normalized = remote_challenge["name"]
            else:
                normalized = r

            return normalized

        return normalize_next(r1) == normalize_next(r2)

    # Normalize challenge data from the API response to match challenge.yml
    # It will remove any extra fields from the remote, as well as expand external references
    # that have to be fetched separately (e.g., files, flags, hints, etc.)
    # Note: files won't be included for two reasons:
    # 1. To avoid downloading them unnecessarily, e.g., when they are ignored
    # 2. Because it's dependent on the implementation whether to save them (mirror) or just compare (verify)
    def _normalize_challenge(self, challenge_data: Dict[str, Any]):
        challenge = {}

        copy_keys = [
            "name",
            "category",
            "attribution",
            "value",
            "type",
            "state",
            "connection_info",
        ]
        for key in copy_keys:
            if key in challenge_data:
                challenge[key] = challenge_data[key]

        challenge["description"] = challenge_data["description"].strip().replace("\r\n", "\n").replace("\t", "")
        challenge["attribution"] = challenge_data.get("attribution", "").strip().replace("\r\n", "\n").replace("\t", "")
        challenge["attempts"] = challenge_data["max_attempts"]

        for key in ["initial", "decay", "minimum"]:
            if key in challenge_data:
                if "extra" not in challenge:
                    challenge["extra"] = {}

                challenge["extra"][key] = challenge_data[key]

        for key in ["latitude", "longitude", "tolerance_radius"]:
            if key in challenge_data:
                if "geo_flags" not in challenge:
                    challenge["geo_flags"] = {}

                challenge["geo_flags"][key] = challenge_data[key]

        # Add flags
        r = self.api.get(f"/api/v1/challenges/{self.challenge_id}/flags")
        r.raise_for_status()
        flags = r.json()["data"]
        challenge["flags"] = [
            (
                f["content"]
                if f["type"] == "static" and (f["data"] is None or f["data"] == "")
                else {
                    "content": f["content"].strip().replace("\r\n", "\n"),
                    "type": f["type"],
                    "data": f["data"],
                }
            )
            for f in flags
        ]

        # Add tags
        r = self.api.get(f"/api/v1/challenges/{self.challenge_id}/tags")
        r.raise_for_status()
        tags = r.json()["data"]
        challenge["tags"] = [t["value"] for t in tags]

        # Add hints
        r = self.api.get(f"/api/v1/challenges/{self.challenge_id}/hints")
        r.raise_for_status()
        hints = r.json()["data"]
        # skipping pre-requisites for hints because they are not supported in ctfcli
        challenge["hints"] = [
            ({"content": h["content"], "cost": h["cost"]} if h["cost"] > 0 else h["content"]) for h in hints
        ]

        # Add topics
        r = self.api.get(f"/api/v1/challenges/{self.challenge_id}/topics")
        r.raise_for_status()
        topics = r.json()["data"]
        challenge["topics"] = [t["value"] for t in topics]

        # Add requirements
        r = self.api.get(f"/api/v1/challenges/{self.challenge_id}/requirements")
        r.raise_for_status()
        requirements = (r.json().get("data") or {}).get("prerequisites", [])
        challenge["requirements"] = {"prerequisites": [], "anonymize": False}
        if len(requirements) > 0:
            # Prefer challenge names over IDs
            r2 = self.api.get("/api/v1/challenges?view=admin")
            r2.raise_for_status()
            challenges = r2.json()["data"]
            challenge["requirements"]["prerequisites"] = [c["name"] for c in challenges if c["id"] in requirements]
        # Add anonymize flag
        challenge["requirements"]["anonymize"] = (r.json().get("data") or {}).get("anonymize", False)

        # Add next
        nid = challenge_data.get("next_id", None)
        if nid:
            # Prefer challenge names over IDs
            r = self.api.get(f"/api/v1/challenges/{nid}")
            r.raise_for_status()
            challenge["next"] = (r.json().get("data") or {}).get("name", None)
        else:
            challenge["next"] = None

        return challenge

    # Create a dictionary of remote files in { basename: {"url": "", "location": ""} } format
    def _normalize_remote_files(self, remote_files: List[str]) -> Dict[str, Dict[str, str]]:
        normalized = {}
        for f in remote_files:
            file_parts = f.split("?token=")[0].split("/")
            normalized[file_parts[-1]] = {
                "url": f,
                "location": f"{file_parts[-2]}/{file_parts[-1]}",
            }

        return normalized

    # Create a dictionary of sha1sums in { location: sha1sum } format
    def _get_files_sha1sums(self) -> Dict[str, str]:
        r = self.api.get("/api/v1/files?type=challenge")
        r.raise_for_status()
        return {f["location"]: f.get("sha1sum", None) for f in r.json()["data"]}

    def sync(self, ignore: Tuple[str] = ()) -> None:
        challenge = self

        if "name" in ignore:
            click.secho(
                "Attribute 'name' cannot be ignored when syncing a challenge",
                fg="yellow",
            )

        if not self.get("name"):
            raise InvalidChallengeFile("Challenge does not provide a name")

        if challenge.get("files", False) and "files" not in ignore:
            # _validate_files will raise if file is not found
            self._validate_files()

        challenge_payload = self._get_initial_challenge_payload(ignore=ignore)

        self._load_challenge_id()
        remote_challenge = self.load_installed_challenge(self.challenge_id)

        # if value, category, type or description are ignored, revert them to the remote state in the initial payload
        reset_properties_if_ignored = [
            "value",
            "category",
            "type",
            "description",
            "attribution",
        ]
        for p in reset_properties_if_ignored:
            if p in ignore:
                challenge_payload[p] = remote_challenge[p]

        # Update simple properties
        r = self.api.patch(f"/api/v1/challenges/{self.challenge_id}", json=challenge_payload)
        r.raise_for_status()

        # Update flags
        if "flags" not in ignore:
            self._delete_existing_flags()
            if challenge.get("flags"):
                self._create_flags()

        # Update topics
        if "topics" not in ignore:
            self._delete_existing_topics()
            if challenge.get("topics"):
                self._create_topics()

        # Update tags
        if "tags" not in ignore:
            self._delete_existing_tags()
            if challenge.get("tags"):
                self._create_tags()

        # Create / Upload files
        if "files" not in ignore:
            self["files"] = self.get("files") or []
            remote_challenge["files"] = remote_challenge.get("files") or []

            # Get basenames of local files to compare against remote files
            local_files = {f.split("/")[-1]: f for f in self["files"]}
            remote_files = self._normalize_remote_files(remote_challenge["files"])

            # Delete remote files which are no longer defined locally
            for remote_file in remote_files:
                if remote_file not in local_files:
                    self._delete_file(remote_files[remote_file]["location"])

            # Only check for file changes if there are files to upload
            if local_files:
                sha1sums = self._get_files_sha1sums()
                for local_file_name in local_files:
                    # Creating a new file
                    if local_file_name not in remote_files:
                        self._create_file(self.challenge_directory / local_files[local_file_name])
                        continue

                    # Updating an existing file
                    # sha1sum is present in CTFd 3.7+, use it instead of always re-uploading the file if possible
                    remote_file_sha1sum = sha1sums[remote_files[local_file_name]["location"]]
                    if remote_file_sha1sum is not None:
                        with open(
                            self.challenge_directory / local_files[local_file_name],
                            "rb",
                        ) as lf:
                            local_file_sha1sum = hash_file(lf)

                        if local_file_sha1sum == remote_file_sha1sum:
                            continue

                    # if sha1sums are not present, or the hashes are different, re-upload the file
                    self._delete_file(remote_files[local_file_name]["location"])
                    self._create_file(self.challenge_directory / local_files[local_file_name])

        # Update hints
        if "hints" not in ignore:
            self._delete_existing_hints()
            if challenge.get("hints"):
                self._create_hints()

        # Update requirements
        if challenge.get("requirements") and "requirements" not in ignore:
            self._set_required_challenges()

        # Set next
        _next = challenge.get("next", None)
        if "next" not in ignore:
            self._set_next(_next)

        make_challenge_visible = False

        # Bring back the challenge to be visible if:
        # 1. State is not ignored and set to visible, or defaults to visible
        if "state" not in ignore:
            if challenge.get("state", "visible") == "visible":
                make_challenge_visible = True

        # 2. State is ignored, but regardless of the local value, the remote state was visible
        else:
            if remote_challenge.get("state") == "visible":
                make_challenge_visible = True

        if make_challenge_visible:
            r = self.api.patch(f"/api/v1/challenges/{self.challenge_id}", json={"state": "visible"})
            r.raise_for_status()

    def create(self, ignore: Tuple[str] = ()) -> None:
        challenge = self

        for attr in ["name", "value"]:
            if attr in ignore:
                click.secho(
                    f"Attribute '{attr}' cannot be ignored when creating a challenge",
                    fg="yellow",
                )

        if not challenge.get("name", False):
            raise InvalidChallengeDefinition("Challenge does not provide a name")

        if not challenge.get("value", False) and challenge.get("type", "standard") != "dynamic":
            raise InvalidChallengeDefinition("Challenge does not provide a value")

        if challenge.get("files", False) and "files" not in ignore:
            # _validate_files will raise if file is not found
            self._validate_files()

        challenge_payload = self._get_initial_challenge_payload(ignore=ignore)

        # in the case of creation, value and type can't be ignored:
        # value is required (unless the challenge is a dynamic value challenge),
        # and the type will default to standard
        # if category or description are ignored, set them to an empty string
        reset_properties_if_ignored = ["category", "description", "attribution"]
        for p in reset_properties_if_ignored:
            if p in ignore:
                challenge_payload[p] = ""

        r = self.api.post("/api/v1/challenges", json=challenge_payload)
        r.raise_for_status()

        self.challenge_id = r.json()["data"]["id"]

        # Create flags
        if challenge.get("flags") and "flags" not in ignore:
            self._create_flags()

        # Create topics
        if challenge.get("topics") and "topics" not in ignore:
            self._create_topics()

        # Create tags
        if challenge.get("tags") and "tags" not in ignore:
            self._create_tags()

        # Upload files
        if challenge.get("files") and "files" not in ignore:
            self._create_all_files()

        # Add hints
        if challenge.get("hints") and "hints" not in ignore:
            self._create_hints()

        # Add requirements
        if challenge.get("requirements") and "requirements" not in ignore:
            self._set_required_challenges()

        # Add next
        _next = challenge.get("next", None)
        if "next" not in ignore:
            self._set_next(_next)

        # Bring back the challenge if it's supposed to be visible
        # Either explicitly, or by assuming the default value (possibly because the state is ignored)
        if challenge.get("state", "visible") == "visible" or "state" in ignore:
            r = self.api.patch(f"/api/v1/challenges/{self.challenge_id}", json={"state": "visible"})
            r.raise_for_status()

    def lint(self, skip_hadolint=False, flag_format="flag{") -> bool:
        challenge = self

        issues = {"fields": [], "dockerfile": [], "hadolint": [], "files": []}

        # Check if required fields are present
        for field in [
            "name",
            "author",
            "category",
            "description",
            "attribution",
            "value",
        ]:
            # value is allowed to be none if the challenge type is dynamic
            if field == "value" and challenge.get("type") == "dynamic":
                continue

            if challenge.get(field) is None:
                issues["fields"].append(f"challenge.yml is missing required field: {field}")

        # Check that the image field and Dockerfile match
        if (self.challenge_directory / "Dockerfile").is_file() and challenge.get("image", "") != ".":
            issues["dockerfile"].append("Dockerfile exists but image field does not point to it")

        # Check that Dockerfile exists and is EXPOSE'ing a port
        if challenge.get("image") == ".":
            dockerfile_path = self.challenge_directory / "Dockerfile"
            has_dockerfile = dockerfile_path.is_file()

            if not has_dockerfile:
                issues["dockerfile"].append("Dockerfile specified in 'image' field but no Dockerfile found")

            if has_dockerfile:
                with open(dockerfile_path, "r") as dockerfile:
                    dockerfile_source = dockerfile.read()

                    if "EXPOSE" not in dockerfile_source:
                        issues["dockerfile"].append("Dockerfile is missing EXPOSE")

                    if not skip_hadolint:
                        # Check Dockerfile with hadolint
                        hadolint = subprocess.run(
                            ["docker", "run", "--rm", "-i", "hadolint/hadolint"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            input=dockerfile_source.encode(),
                        )

                        if hadolint.returncode != 0:
                            issues["hadolint"].append(hadolint.stdout.decode())

                    else:
                        click.secho("Skipping Hadolint", fg="yellow")

        # Check that all files exist
        files = self.get("files") or []
        for challenge_file in files:
            challenge_file_path = self.challenge_directory / challenge_file

            if challenge_file_path.is_file() is False:
                issues["files"].append(
                    f"Challenge file '{challenge_file}' specified, but not found at {challenge_file_path}"
                )

        # Check that files don't have a flag in them
        for challenge_file in files:
            challenge_file_path = self.challenge_directory / challenge_file

            if not challenge_file_path.exists():
                # The check for files present is above; this is only to look for flags in files that we do have
                continue

            for s in strings(challenge_file_path):
                if flag_format in s:
                    s = s.strip()
                    issues["files"].append(f"Potential flag found in distributed file '{challenge_file}':\n {s}")

        if any(messages for messages in issues.values() if len(messages) > 0):
            raise LintException(issues=issues)

        return True

    def mirror(self, files_directory_name: str = "dist", ignore: Tuple[str] = ()) -> None:
        self._load_challenge_id()
        remote_challenge = self.load_installed_challenge(self.challenge_id)
        challenge = self._normalize_challenge(remote_challenge)

        remote_challenge["files"] = remote_challenge.get("files") or []
        challenge["files"] = challenge.get("files") or []

        # Add files which are not handled in _normalize_challenge
        if "files" not in ignore:
            local_files = {Path(f).name: f for f in challenge["files"]}

            # Update files
            for remote_file in remote_challenge["files"]:
                # Get base file name
                remote_file_name = remote_file.split("/")[-1].split("?token=")[0]

                # The file is only present on the remote - we have to download it, and assume a path
                if remote_file_name not in local_files:
                    r = self.api.get(remote_file)
                    r.raise_for_status()

                    # Ensure the directory for the challenge files exists
                    challenge_files_directory = self.challenge_directory / files_directory_name
                    challenge_files_directory.mkdir(parents=True, exist_ok=True)

                    (challenge_files_directory / remote_file_name).write_bytes(r.content)
                    challenge["files"].append(f"{files_directory_name}/{remote_file_name}")

                # The file is already present in the challenge.yml - we know the desired path
                else:
                    r = self.api.get(remote_file)
                    r.raise_for_status()
                    (self.challenge_directory / local_files[remote_file_name]).write_bytes(r.content)

            # Soft-Delete files that are not present on the remote
            # Remove them from challenge.yml but do not delete them from disk
            remote_file_names = [f.split("/")[-1].split("?token=")[0] for f in remote_challenge["files"]]
            challenge["files"] = [f for f in challenge["files"] if Path(f).name in remote_file_names]

        for key in challenge.keys():
            if key not in ignore:
                self[key] = challenge[key]

        self.save()

    def verify(self, ignore: Tuple[str] = ()) -> bool:
        self._load_challenge_id()
        challenge = self
        remote_challenge = self.load_installed_challenge(self.challenge_id)
        normalized_challenge = self._normalize_challenge(remote_challenge)

        remote_challenge["files"] = remote_challenge.get("files") or []
        challenge["files"] = challenge.get("files") or []

        for key in normalized_challenge:
            if key in ignore:
                continue

            # If challenge.yml doesn't have some property from the remote
            # Check if it's a default value that can be omitted
            if key not in challenge:
                if self.is_default_challenge_property(key, normalized_challenge[key]):
                    continue

                click.secho(
                    f"{key} is not in challenge.",
                    fg="yellow",
                )

                return False

            if challenge[key] != normalized_challenge[key]:
                if key == "requirements":
                    if type(challenge[key]) == dict:
                        cr = challenge[key]["prerequisites"]
                        ca = challenge[key].get("anonymize", False)
                    else:
                        cr = challenge[key]
                        ca = False
                    if self._compare_challenge_requirements(cr, normalized_challenge[key]["prerequisites"]):
                        if ca == normalized_challenge[key]["anonymize"]:
                            continue

                if key == "next":
                    if self._compare_challenge_next(challenge[key], normalized_challenge[key]):
                        continue

                click.secho(
                    f"{key} comparison failed.",
                    fg="yellow",
                )

                return False

        # Handle a special case for files, unless they are ignored
        if "files" not in ignore:
            # Check if files defined in challenge.yml are present
            try:
                self._validate_files()
                local_files = {Path(f).name: f for f in challenge["files"]}
            except InvalidChallengeFile:
                click.secho(
                    "InvalidChallengeFile",
                    fg="yellow",
                )
                return False

            remote_files = self._normalize_remote_files(remote_challenge["files"])
            # Check if there are no extra local files
            for local_file in local_files:
                if local_file not in remote_files:
                    click.secho(
                        f"{local_file} is not in remote challenge.",
                        fg="yellow",
                    )
                    return False

            sha1sums = self._get_files_sha1sums()
            # Check if all remote files are present locally
            for remote_file_name in remote_files:
                if remote_file_name not in local_files:
                    click.secho(
                        f"{remote_file_name} is not in local challenge.",
                        fg="yellow",
                    )
                    return False

                # sha1sum is present in CTFd 3.7+, use it instead of downloading the file if possible
                remote_file_sha1sum = sha1sums[remote_files[remote_file_name]["location"]]
                if remote_file_sha1sum is not None:
                    with open(self.challenge_directory / local_files[remote_file_name], "rb") as lf:
                        local_file_sha1sum = hash_file(lf)

                    if local_file_sha1sum != remote_file_sha1sum:
                        click.secho(
                            "sha1sum does not match with remote one.",
                            fg="yellow",
                        )
                        return False

                    return True

                # If sha1sum is not present, download the file and compare the contents
                r = self.api.get(remote_files[remote_file_name]["url"])
                r.raise_for_status()
                remote_file_contents = r.content
                local_file_contents = (self.challenge_directory / local_files[remote_file_name]).read_bytes()

                if remote_file_contents != local_file_contents:
                    click.secho(
                        "the file content does not match with the remote one.",
                        fg="yellow",
                    )
                    return False

        return True

    def save(self):
        challenge_dict = dict(self)

        # sort the challenge dict by the key order defined from the spec
        # also strip any default values
        sorted_challenge_dict = {
            k: challenge_dict[k]
            for k in self.key_order
            if k in challenge_dict and not self.is_default_challenge_property(k, challenge_dict[k])
        }

        # if there are any additional keys append them at the end
        unknown_keys = set(challenge_dict) - set(self.key_order)
        for k in unknown_keys:
            sorted_challenge_dict[k] = challenge_dict[k]

        try:
            challenge_yml = yaml.safe_dump(sorted_challenge_dict, sort_keys=False, allow_unicode=True)

            # attempt to pretty print the yaml (add an extra newline between selected top-level keys)
            pattern = "|".join(r"^" + re.escape(key) + r":" for key in self.keys_with_newline)
            pretty_challenge_yml = re.sub(pattern, r"\n\g<0>", challenge_yml, flags=re.MULTILINE)

            with open(self.challenge_file_path, "w") as challenge_file:
                challenge_file.write(pretty_challenge_yml)

        except Exception as e:
            raise InvalidChallengeFile(f"Challenge file could not be saved:\n{e}")
