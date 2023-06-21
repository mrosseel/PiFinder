import csv

# Specify the names of the input and output files
input_file_name = "herschel400.dat"
output_file_name = "herschel400.csv"

# Read the input file
with open(input_file_name, "r") as in_file:
    lines = in_file.readlines()

# Prepare the data for the CSV file
data = [line.split() for line in lines]

# Write the data to the CSV file
with open(output_file_name, "w", newline="") as out_file:
    writer = csv.writer(out_file)
    writer.writerows(data)
