from wam import wizardmd


def test_home_network_is_a_known_need():
    assert "home-network" in wizardmd.NEEDS
