from hyundai_kia_connect_api import VehicleManager

# Region codes: 1 = USA, 2 = Europe, 3 = Canada
# Brand codes: 1 = Kia, 2 = Hyundai
vm = VehicleManager(
    region=1, 
    brand=1, 
    username="grahamjones139@gmail.com", 
    password="cxFx72LFFp_2g3b", 
    pin=""
)

# Fetch current vehicle state from Kia Connect servers
vm.check_and_refresh_token()
vm.update_all_vehicles_with_cached_state()

# Query specific vehicle details
for vin, vehicle in vm.vehicles.items():
    print(f"Vehicle: {vehicle.name} ({vin})")
    print(f"Odometer: {vehicle.odometer} km")
    print(f"EV Battery Level: {vehicle.ev_battery_percentage}%")
    print(f"Location: Lat {vehicle.location_latitude}, Lon {vehicle.location_longitude}")
    print(f"Locked: {vehicle.is_locked}")
    print(f"Tyre Pressures: {vehicle.tire_pressure_front_left},{vehicle.tire_pressure_front_right},{vehicle.tire_pressure_rear_left},{vehicle.tire_pressure_rear_right}")
    print(f"Battery State of Health: {vehicle.ev_battery_soh_percentage}")
    print(f"Battery Capacity: {vehicle.ev_battery_capacity}")
    print(vehicle)

    for day in vehicle._daily_stats:
        print(f"Date: {day.date}: Consumption: {day.total_consumed/1000} kWh, distance {day.distance} km, efficiency = {1000*day.distance/1.6/day.total_consumed}")
