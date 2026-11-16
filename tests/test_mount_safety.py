from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import mount_safety


def encoded(value):
    return (str(value).replace("\\", "\\134").replace(" ", "\\040")
            .replace("\t", "\\011").replace("\n", "\\012"))


def mount(identifier, point, *, device="8:1", root="/", filesystem="ext4", parent=1):
    return (f"{identifier} {parent} {device} {encoded(root)} {encoded(point)} "
            f"rw,relatime shared:1 - {filesystem} /dev/example rw\n")


class MountSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.inventory = self.work / "mountinfo"
        self.target = self.work / "data" / "clean" / "mysql-2026-10-01"
        self.target.mkdir(parents=True)
        self.base = mount(1, "/", parent=0)

    def check(self, *entries, target=None):
        self.inventory.write_text(self.base + "".join(entries))
        return mount_safety.assert_cleanup_unmounted(
            target or self.target, mountinfo_path=self.inventory,
        )

    def assert_blocked(self, *entries, target=None, message=None):
        with self.assertRaisesRegex(mount_safety.MountSafetyError, message or ""):
            self.check(*entries, target=target)

    def test_ordinary_root_filesystem_ancestor_is_allowed(self):
        self.check()

    def test_dedicated_data_filesystem_ancestor_is_allowed(self):
        self.check(mount(2, self.work / "data", device="259:2"))

    def test_same_coordinate_self_bind_ancestor_is_not_an_alias(self):
        data = self.work / "data"
        self.check(mount(2, data, root=data))

    def test_direct_filesystem_mount_is_rejected(self):
        self.assert_blocked(mount(2, self.target, device="259:2"), message="mountpoint")

    def test_direct_bind_mount_is_rejected(self):
        self.assert_blocked(mount(2, self.target, root="/old/serving"), message="mountpoint")

    def test_nested_mount_is_rejected(self):
        self.assert_blocked(mount(2, self.target / "database", device="259:2"), message="contains a mount")

    def test_file_bind_mount_is_rejected(self):
        artifact = self.target / "records.ibd"
        artifact.touch()
        self.assert_blocked(mount(2, artifact, root="/elsewhere/records.ibd"),
                            target=artifact, message="mountpoint")

    def test_exact_bind_source_alias_is_rejected(self):
        self.assert_blocked(mount(2, "/elsewhere", root=self.target), message="mount alias")

    def test_parent_bind_source_alias_is_rejected(self):
        self.assert_blocked(mount(2, "/elsewhere", root=self.target.parent), message="mount alias")

    def test_descendant_bind_source_alias_is_rejected(self):
        self.assert_blocked(mount(2, "/elsewhere", root=self.target / "database"), message="mounted subtree")

    def test_whole_filesystem_alternate_mount_is_rejected(self):
        self.assert_blocked(mount(2, "/elsewhere"), message="mount alias")

    def test_unrelated_same_device_subtree_is_allowed(self):
        self.check(mount(2, "/elsewhere", root="/unrelated"))

    def test_unrelated_other_device_root_is_allowed(self):
        self.check(mount(2, "/elsewhere", device="8:2"))

    def test_prefix_matching_does_not_confuse_siblings(self):
        self.check(mount(2, self.target.with_name(self.target.name + "-unrelated"), root="/other"))

    def test_filesystem_relative_bind_source_is_detected(self):
        data = self.work / "data"
        for root in ("/clean/mysql-2026-10-01", "/clean", "/clean/mysql-2026-10-01/database"):
            with self.subTest(root=root):
                self.assert_blocked(mount(2, data, device="0:30", filesystem="zfs"),
                                    mount(3, "/elsewhere", device="0:30", root=root, filesystem="zfs"))

    def test_subtree_mount_root_coordinates_are_honoured(self):
        data = self.work / "data"
        self.assert_blocked(mount(2, data, device="8:2", root="/tenants/ojs"),
                            mount(3, "/elsewhere", device="8:2", root="/tenants/ojs/clean"))

    def test_path_escapes_are_decoded(self):
        for suffix in ("space here", "tab\there", "newline\nhere", "backslash\\here"):
            target = self.work / suffix
            target.mkdir()
            with self.subTest(suffix=suffix):
                self.assert_blocked(mount(2, "/elsewhere", root=target), target=target)

    def test_namespace_handle_outside_target_is_allowed(self):
        for namespace in ("mnt", "net", "user", "time_for_children"):
            with self.subTest(namespace=namespace):
                self.check(mount(2, "/run/example.ns", device="0:4",
                                 root=f"{namespace}:[123456]", filesystem="nsfs"))

    def test_namespace_handle_nested_under_target_is_rejected(self):
        self.assert_blocked(mount(2, self.target / "namespace", device="0:4",
                                  root="mnt:[123456]", filesystem="nsfs"))

    def test_relative_non_namespace_root_fails_closed(self):
        self.assert_blocked(mount(2, "/elsewhere", device="8:2", root="relative"))

    def test_invalid_namespace_handle_fails_closed(self):
        self.assert_blocked(mount(2, "/elsewhere", device="0:4", root="mnt:[invalid]", filesystem="nsfs"))

    def test_unrecognized_path_escape_fails_closed(self):
        self.inventory.write_text(self.base + "2 1 8:2 / /wrong\\999path rw - ext4 /dev/example rw\n")
        with self.assertRaises(mount_safety.MountSafetyError):
            mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.inventory)

    def test_parent_path_component_in_mount_coordinates_fails_closed(self):
        self.assert_blocked(mount(2, "/elsewhere", device="8:2", root="/data/../private"))

    def test_ambiguous_containing_overmount_fails_closed(self):
        self.assert_blocked(mount(2, self.work / "data", device="8:2"),
                            mount(3, self.work / "data", device="8:3"), message="ambiguous")

    def test_stacked_identical_containing_views_are_allowed(self):
        self.check(mount(2, self.work / "data", device="8:2"),
                   mount(3, self.work / "data", device="8:2", parent=2))

    def test_duplicate_mount_identifier_fails_closed(self):
        self.assert_blocked(mount(1, "/elsewhere", device="8:2"), message="duplicate")

    def test_inventory_without_containing_filesystem_fails_closed(self):
        self.inventory.write_text(mount(1, "/elsewhere", parent=0))
        with self.assertRaisesRegex(mount_safety.MountSafetyError, "absent"):
            mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.inventory)

    def test_missing_inventory_fails_closed_without_private_path(self):
        with self.assertRaisesRegex(mount_safety.MountSafetyError, "unavailable") as caught:
            mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.work / "secret-private-mounts")
        self.assertNotIn("secret-private", str(caught.exception))

    def test_unreadable_inventory_fails_closed(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError("private mount path")):
            with self.assertRaisesRegex(mount_safety.MountSafetyError, "unavailable") as caught:
                mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.inventory)
        self.assertNotIn("private mount path", str(caught.exception))

    def test_empty_malformed_and_non_utf8_inventory_fail_closed(self):
        for content in (b"", b"\n", b"garbage\n", b"1 0 8:1 / / rw ext4\n", b"\xff"):
            with self.subTest(content=content):
                self.inventory.write_bytes(content)
                with self.assertRaises(mount_safety.MountSafetyError):
                    mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.inventory)

    def test_oversized_inventory_fails_closed(self):
        self.inventory.write_text(self.base)
        with mock.patch.object(mount_safety, "_MAX_MOUNTINFO_BYTES", 8):
            with self.assertRaisesRegex(mount_safety.MountSafetyError, "oversized"):
                mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.inventory)

    def test_target_symlink_is_rejected(self):
        link = self.work / "alias"
        link.symlink_to(self.target, target_is_directory=True)
        self.assert_blocked(target=link, message="non-symlink")

    def test_relative_target_is_rejected(self):
        self.assert_blocked(target=Path("relative"), message="absolute")

    def test_inventory_is_reread_between_planning_and_deletion(self):
        self.check()
        self.inventory.write_text(self.base + mount(2, self.target, device="8:2"))
        with self.assertRaisesRegex(mount_safety.MountSafetyError, "mountpoint"):
            mount_safety.assert_cleanup_unmounted(self.target, mountinfo_path=self.inventory)


if __name__ == "__main__":
    unittest.main()
